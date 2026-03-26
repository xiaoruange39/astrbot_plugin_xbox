import io
import httpx
import math
import asyncio
import os
import subprocess
import platform
from dataclasses import dataclass
from urllib.parse import urlparse
from PIL import Image, ImageDraw, ImageFont
from astrbot.api import logger

# Pillow global pixel limit to prevent decompression bombs
Image.MAX_IMAGE_PIXELS = 25_000_000

# Allowed image host domains (Microsoft CDN)
_ALLOWED_IMAGE_HOSTS = {
    "store-images.s-microsoft.com",
    "store-images.microsoft.com",
    "images-eds-ssl.xboxlive.com",
    "musicimage.xboxlive.com",
    "images-eds.xboxlive.com",
}

# Define green color for XGP
XGP_GREEN = "#0f7e17" # Deeper official green background
WHITE = "#FFFFFF"
LIGHT_GREEN = "#9bf00b" # Bright green for platforms
BADGE_GREEN = "#00a300"


@dataclass(frozen=True)
class RenderLayout:
    """UI layout constants for announcement image rendering."""
    poster_w: int = 300
    poster_h: int = 450
    spacing: int = 80
    padding_top: int = 320
    padding_bottom: int = 420
    padding_side: int = 120
    items_per_row_max: int = 6
    title_y: int = 90
    title_below_poster: int = 30
    tier_below_poster: int = 85
    platform_below_tier: int = 35
    poster_corner_radius: int = 16
    border_radius: int = 18
    poster_border_width: int = 2
    badge_logo_w: int = 220
    badge_logo_h: int = 176
    badge_logo_radius: int = 24
    badge_offset_x: int = 40
    badge_offset_y: int = 200
    badge_icon_center_y: int = 48
    badge_xbox_text_y: int = 88
    badge_gp_text_y: int = 123
    row_gap: int = 220
    zh_badge_pad_h: int = 24
    zh_badge_pad_v: int = 10
    zh_badge_margin_bottom: int = 10
    zh_badge_slant: int = 20
    footer_bottom_margin: int = 40

def find_chinese_font():
    """Locate a Chinese-capable font on the system.

    Note: This runs synchronous subprocess calls, but is only invoked once
    at module/class init time, not on the hot request path.
    """
    # 1. Check local directory first
    current_dir = os.path.dirname(os.path.abspath(__file__))
    for local_font in ["font.ttf", "font.ttc", "font.otf"]:
        p = os.path.join(current_dir, local_font)
        if os.path.exists(p):
            return p

    # 2. Try using fc-list on Linux/macOS to find ANY font supporting Chinese
    if platform.system() != "Windows":
        try:
            result = subprocess.run(
                ['fc-list', ':lang=zh', 'file'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0 and result.stdout:
                fonts = result.stdout.strip().split('\n')
                for line in fonts:
                    file_path = line.split(':')[0].strip()
                    if file_path.lower().endswith(('.ttf', '.ttc', '.otf')):
                        if os.path.exists(file_path):
                            return file_path
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"fc-list font search failed: {e}")

    # 3. Fallback to hardcoded common paths if fc-list fails or on Windows
    common_fonts = [
        # Windows
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        # Common Linux
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
    ]
    for path in common_fonts:
        if os.path.exists(path):
            return path

    # 4. Final desperate fallback -> try to just load by generic name
    for name in ["msyh.ttc", "simhei.ttf", "NotoSansCJK-Regular.ttc"]:
        try:
            ImageFont.truetype(name, 10)
            return name
        except IOError:
            pass

    logger.warning("Absolutely no Chinese font could be found on your system!")
    return None


class XGPImageGenerator:
    def __init__(self):
        self.font_path = find_chinese_font()
        self._client: httpx.AsyncClient | None = None
        self._download_semaphore = asyncio.Semaphore(6)
        self._render_sem = asyncio.Semaphore(2)  # Limit concurrent render threads
        self.layout = RenderLayout()
        # Adjusted font sizes per user feedback
        self.font_title = self._get_font(70)
        self.font_game_title = self._get_font(34)
        self.font_platform = self._get_font(22)
        self.font_tier = self._get_font(20) # Subscription tier label
        self.font_badge = self._get_font(24)
        self.font_logo = self._get_font(38)
        self.font_footer = self._get_font(16)

        # Load the high-res user SVG rendered to PNG
        self.xbox_logo_path = os.path.join(os.path.dirname(__file__), "xbox_logo.png")
        self.xbox_icon = None
        if os.path.exists(self.xbox_logo_path):
            try:
                with Image.open(self.xbox_logo_path) as img_logo_raw:
                    img_logo = img_logo_raw.convert("RGBA")
                # Make white background transparent if reportlab didn't set alpha
                data = img_logo.getdata()
                new_data = []
                for item in data:
                    if item[0] > 250 and item[1] > 250 and item[2] > 250:
                        new_data.append((255, 255, 255, 0)) # transparent
                    else:
                        new_data.append(item)
                img_logo.putdata(new_data)
                self.xbox_icon = img_logo.resize((75, 75), Image.Resampling.LANCZOS)
                img_logo.close()
            except (OSError, ValueError) as e:
                logger.debug(f"Failed to load Xbox logo: {e}")

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazy-initialized async HTTP client (safe across event loop lifecycles)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def close(self):
        """Close the shared HTTP client and free internal icon memory."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self.xbox_icon:
            self.xbox_icon.close()

    def _draw_vertical_gradient(self, width, height, top_hex, bottom_hex):
        base = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(base)
        
        def hex_to_rgb(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
            
        r1, g1, b1 = hex_to_rgb(top_hex)
        r2, g2, b2 = hex_to_rgb(bottom_hex)
        
        divisor = max(height - 1, 1)
        for y in range(height):
            ratio = y / divisor
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
        return base


    def _get_font(self, size):
        if self.font_path:
            try:
                return ImageFont.truetype(self.font_path, size)
            except IOError:
                pass
        return ImageFont.load_default()

    async def _download_image(self, url: str) -> "Image.Image | None":
        """Download and open an image with size, pixel, and domain safeguards."""
        max_content_bytes = 10 * 1024 * 1024  # 10 MB
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_IMAGE_HOSTS:
                logger.debug(f"Blocked image download from untrusted host: {url}")
                return None
            async with self._download_semaphore:
                resp = await self.client.get(url)
                resp.raise_for_status()
                content_len = resp.headers.get("content-length")
                if content_len and int(content_len) > max_content_bytes:
                    logger.debug(f"Image too large ({content_len} bytes), skipping: {url}")
                    return None
                if len(resp.content) > max_content_bytes:
                    logger.debug(f"Image body too large ({len(resp.content)} bytes), skipping: {url}")
                    return None
                raw_img = Image.open(io.BytesIO(resp.content))
                converted = raw_img
                try:
                    converted = raw_img.convert("RGBA")
                finally:
                    # close the original if convert() created a new object
                    if converted is not raw_img:
                        raw_img.close()
                return converted
        except Image.DecompressionBombError:
            logger.warning(f"Decompression bomb detected, skipping: {url}")
            return None
        except httpx.HTTPError as e:
            logger.debug(f"Failed to download image {url}: {e}")
            return None
        except (OSError, ValueError) as e:
            logger.debug(f"Failed to open downloaded image {url}: {e}")
            return None

    def _crop_to_ratio(self, img: Image.Image, target_ratio: float) -> Image.Image:
        """Crop image to match exactly target_width/target_height ratio from the center.
        Closes the original image if a new cropped copy is created."""
        w, h = img.size
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            cropped = img.crop((left, 0, left + new_w, h))
            img.close()
            return cropped
        elif current_ratio < target_ratio:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            cropped = img.crop((0, top, w, top + new_h))
            img.close()
            return cropped
        return img

    def _add_rounded_corners(self, img: Image.Image, radius: int) -> Image.Image:
        """Add rounded corners to an image. Closes the original and intermediate mask."""
        mask = Image.new('L', img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)

        result = img.copy()
        if result.mode != 'RGBA':
            unconverted = result
            result = result.convert('RGBA')
            unconverted.close()
        result.putalpha(mask)
        mask.close()
        img.close()
        return result

    async def generate_announcement_image(self, title: str, games: list[dict]) -> bytes:
        """
        Generate a horizontally laid out announcement image.
        Downloads images concurrently, then offloads CPU-intensive rendering to a thread.
        All downloaded poster images are explicitly closed after rendering.
        """
        num_games = len(games)
        if num_games == 0:
            return b""

        # Fetch all images concurrently (I/O bound, stays in event loop)
        async def fetch_poster(game):
            url = game.get("image_url")
            if url:
                for _ in range(2):
                    img = await self._download_image(url)
                    if img:
                        return img
            return None

        # Log and replace exceptions with None so rendering can continue
        posters_raw = []
        for i, p in enumerate(await asyncio.gather(
            *(fetch_poster(g) for g in games), return_exceptions=True
        )):
            if isinstance(p, Exception):
                logger.debug(f"Poster download failed for '{games[i].get('title', '?')}': {p}")
                posters_raw.append(None)
            elif isinstance(p, Image.Image):
                posters_raw.append(p)
            else:
                posters_raw.append(None)

        # Offload CPU-intensive Pillow rendering to a thread (bounded concurrency)
        # _render_image is responsible for closing all poster images in posters_raw
        try:
            async with self._render_sem:
                return await asyncio.to_thread(self._render_image, title, games, posters_raw)
        except Exception:
            # If rendering thread failed to start, clean up posters here
            for img in posters_raw:
                if img is not None:
                    img.close()
            raise

    def _draw_game_card(self, draw: ImageDraw.Draw, base_img: Image.Image,
                        game: dict, poster_raw: "Image.Image | None", x: int, y: int):
        """Draw a single game card: poster, title, tier label, and platform text."""
        L = self.layout
        bw = L.poster_border_width

        poster = None
        try:
            if poster_raw:
                poster = self._crop_to_ratio(poster_raw, 2 / 3)
                resized = poster.resize((L.poster_w, L.poster_h), Image.Resampling.LANCZOS)
                poster.close()
                poster = resized
            else:
                poster = Image.new("RGBA", (L.poster_w, L.poster_h), "#333333")
                d = ImageDraw.Draw(poster)
                placeholder = "无封面"
                v_box = d.textbbox((0, 0), placeholder, font=self.font_game_title)
                d.text(
                    ((L.poster_w - (v_box[2] - v_box[0])) // 2, (L.poster_h - (v_box[3] - v_box[1])) // 2),
                    placeholder, font=self.font_game_title, fill=WHITE,
                )

            poster = self._add_rounded_corners(poster, L.poster_corner_radius)
            draw.rounded_rectangle(
                [x - bw, y - bw, x + L.poster_w + bw, y + L.poster_h + bw],
                radius=L.border_radius, fill=WHITE,
            )
            base_img.paste(poster, (x, y), mask=poster)
        finally:
            if poster is not None:
                poster.close()

        # Chinese badge
        if game.get("has_zh"):
            self._draw_zh_badge(draw, x, y)

        # Game title (truncated to poster width)
        g_title = game["title"]
        max_width = L.poster_w
        if draw.textlength(g_title, font=self.font_game_title) > max_width:
            while len(g_title) > 0 and draw.textlength(g_title + "...", font=self.font_game_title) > max_width:
                g_title = g_title[:-1]
            g_title += "..."
        draw.text((x, y + L.poster_h + L.title_below_poster), g_title, font=self.font_game_title, fill=WHITE, stroke_width=1)

        # Subscription tier
        tier_text = game.get("tier", "ULTIMATE").upper()
        tier_font = self.font_tier
        if draw.textlength(tier_text, font=tier_font) > L.poster_w:
            if " \u00b7 " in tier_text:
                temp_font = self._get_font(18)
                if draw.textlength(tier_text, font=temp_font) <= L.poster_w:
                    tier_font = temp_font
            if draw.textlength(tier_text, font=tier_font) > L.poster_w:
                while draw.textlength(tier_text + "...", font=tier_font) > L.poster_w and len(tier_text) > 0:
                    tier_text = tier_text[:-1]
                tier_text += "..."
        tier_y = y + L.poster_h + L.tier_below_poster
        draw.text((x, tier_y), tier_text, font=tier_font, fill=WHITE)

        # Platforms
        platform_text = game.get("platforms", "主机").replace("主机", "主 机")
        if draw.textlength(platform_text, font=self.font_platform) > L.poster_w:
            while draw.textlength(platform_text + "...", font=self.font_platform) > L.poster_w and len(platform_text) > 0:
                platform_text = platform_text[:-1]
            platform_text += "..."
        draw.text((x, tier_y + L.platform_below_tier), platform_text, font=self.font_platform, fill=LIGHT_GREEN, stroke_width=1)

    def _draw_zh_badge(self, draw: ImageDraw.Draw, x: int, y: int):
        """Draw the '支持中文' parallelogram badge on the bottom-right of a poster."""
        L = self.layout
        badge_text = "支持中文"
        badge_bbox = draw.textbbox((0, 0), badge_text, font=self.font_badge)
        b_w, b_h = badge_bbox[2] - badge_bbox[0], badge_bbox[3] - badge_bbox[1]

        banner_h = b_h + L.zh_badge_pad_v * 2
        banner_w = b_w + L.zh_badge_pad_h * 2

        p_x1 = x + L.poster_w - banner_w
        p_x2 = x + L.poster_w
        p_y1 = y + L.poster_h - banner_h - L.zh_badge_margin_bottom
        p_y2 = y + L.poster_h - L.zh_badge_margin_bottom

        poly = [
            (p_x1 + L.zh_badge_slant, p_y1),
            (p_x2, p_y1),
            (p_x2, p_y2),
            (p_x1, p_y2)
        ]
        draw.polygon(poly, fill=BADGE_GREEN)

        text_center_x = p_x1 + L.zh_badge_slant + (banner_w - L.zh_badge_slant) / 2
        text_center_y = p_y1 + banner_h / 2
        draw.text((text_center_x, text_center_y), badge_text, font=self.font_badge, fill=WHITE, stroke_width=1, anchor="mm")

    def _draw_logo_badge(self, draw: ImageDraw.Draw, base_img: Image.Image, img_w: int, img_h: int):
        """Draw the Xbox Game Pass logo badge at the bottom-right."""
        L = self.layout
        badge_w, badge_h = L.badge_logo_w, L.badge_logo_h
        bx = img_w - L.padding_side - badge_w + L.badge_offset_x
        by = img_h - L.padding_bottom + L.badge_offset_y

        draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=L.badge_logo_radius, fill=WHITE)

        cx, cy = bx + badge_w / 2, by + L.badge_icon_center_y
        if self.xbox_icon:
            icon_w, icon_h = self.xbox_icon.size
            base_img.paste(self.xbox_icon, (int(cx - icon_w / 2), int(cy - icon_h / 2)), self.xbox_icon)
        else:
            r = 30
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#1a1a1a")
            draw.chord([cx - r - 8, cy - r - 40, cx + r + 8, cy + r - 20], 50, 130, fill=WHITE)
            draw.chord([cx - r - 8, cy - r + 20, cx + r + 8, cy + r + 40], 230, 310, fill=WHITE)
            offset = 20
            draw.chord([cx - r - 40, cy - r - 10, cx + r - offset, cy + r + 10], 320, 40, fill=WHITE)
            draw.chord([cx - r + offset, cy - r - 10, cx + r + 40, cy + r + 10], 140, 220, fill=WHITE)

        xbox_w = draw.textlength("Xbox", font=self.font_logo)
        draw.text((cx - xbox_w / 2, by + L.badge_xbox_text_y), "Xbox", font=self.font_logo, fill="#1a1a1a", stroke_width=1)

        gp_text = "Game Pass"
        gp_w = draw.textlength(gp_text, font=self.font_logo)
        draw.text((cx - gp_w / 2, by + L.badge_gp_text_y), gp_text, font=self.font_logo, fill="#1a1a1a", stroke_width=1)

    def _draw_footer(self, draw: ImageDraw.Draw, img_w: int, img_h: int):
        """Draw the footer text centered at the bottom."""
        L = self.layout
        footer = "Xbox Game Pass 入库提醒 \u00b7 by xiaoruange39 \u00b7 Powered by AstrBot"
        footer_w = draw.textlength(footer, font=self.font_footer)
        draw.text(((img_w - footer_w) / 2, img_h - L.footer_bottom_margin), footer, font=self.font_footer, fill="#88cc88")

    def _render_image(self, title: str, games: list[dict], posters_raw: list) -> bytes:
        """CPU-intensive image rendering, runs in a thread pool.
        Responsible for closing all images in posters_raw when done."""
        L = self.layout
        num_games = len(games)

        items_per_row = min(num_games, L.items_per_row_max)
        num_rows = math.ceil(num_games / items_per_row)

        img_w = L.padding_side * 2 + items_per_row * L.poster_w + (items_per_row - 1) * L.spacing
        img_h = L.padding_top + L.padding_bottom + num_rows * L.poster_h + (num_rows - 1) * L.row_gap

        base_img = self._draw_vertical_gradient(int(img_w), int(img_h), XGP_GREEN, "#084d08")
        try:
            draw = ImageDraw.Draw(base_img)

            # Draw main title centered
            title_bbox = draw.textbbox((0, 0), title, font=self.font_title)
            title_w = title_bbox[2] - title_bbox[0]
            draw.text(((img_w - title_w) // 2, L.title_y), title, font=self.font_title, fill=WHITE, stroke_width=2)

            # Draw game cards
            for idx, game in enumerate(games):
                row = idx // items_per_row
                col = idx % items_per_row
                x = L.padding_side + col * (L.poster_w + L.spacing)
                y = L.padding_top + row * (L.poster_h + L.row_gap)

                raw_img = posters_raw[idx]
                posters_raw[idx] = None  # transfer ownership
                self._draw_game_card(draw, base_img, game, raw_img, x, y)

            self._draw_logo_badge(draw, base_img, img_w, img_h)
            self._draw_footer(draw, img_w, img_h)

            # Save to JPEG
            output = io.BytesIO()
            rgb_img = base_img.convert("RGB")
            try:
                rgb_img.save(output, format="JPEG", quality=85)
            finally:
                rgb_img.close()
            output.seek(0)
            return output.getvalue()
        finally:
            base_img.close()
            # Clean up any remaining poster images not yet closed
            for img in posters_raw:
                if img is not None:
                    try:
                        img.close()
                    except Exception:
                        pass
