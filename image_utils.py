import io
import httpx
import math
import asyncio
import os
import subprocess
import platform
from PIL import Image, ImageDraw, ImageFont

# Define green color for XGP
XGP_GREEN = "#0f7e17" # Deeper official green background
WHITE = "#FFFFFF"
LIGHT_GREEN = "#9bf00b" # Bright green for platforms
BADGE_GREEN = "#00a300" 
class XGPImageGenerator:
    def __init__(self):
        self.font_path = self._find_chinese_font()
        # Adjusted font sizes per user feedback
        self.font_title = self._get_font(70) 
        self.font_game_title = self._get_font(34)
        self.font_platform = self._get_font(22)
        self.font_tier = self._get_font(20) # Subscription tier label
        self.font_badge = self._get_font(24)
        self.font_logo = self._get_font(38)
        self.font_logo_small = self._get_font(38) # Same size as requested
        self.font_footer = self._get_font(16)

        # Load the high-res user SVG rendered to PNG
        self.xbox_logo_path = os.path.join(os.path.dirname(__file__), "xbox_logo.png")
        self.xbox_icon = None
        if os.path.exists(self.xbox_logo_path):
            try:
                # Load and prepare image (handling transparency if generated with white bg)
                img_logo = Image.open(self.xbox_logo_path).convert("RGBA")
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
            except Exception:
                pass

    def _draw_vertical_gradient(self, width, height, top_hex, bottom_hex):
        base = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(base)
        
        def hex_to_rgb(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
            
        r1, g1, b1 = hex_to_rgb(top_hex)
        r2, g2, b2 = hex_to_rgb(bottom_hex)
        
        for y in range(height):
            r = int(r1 + (r2 - r1) * y / height)
            g = int(g1 + (g2 - g1) * y / height)
            b = int(b1 + (b2 - b1) * y / height)
            draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
        return base

    def _find_chinese_font(self):
        # 1. Check local directory first
        current_dir = os.path.dirname(os.path.abspath(__file__))
        for local_font in ["font.ttf", "font.ttc", "font.otf"]:
            p = os.path.join(current_dir, local_font)
            if os.path.exists(p):
                return p
                
        # 2. Try using fc-list on Linux/macOS to find ANY font supporting Chinese
        if platform.system() != "Windows":
            try:
                # Ask fontconfig for fonts supporting Chinese (lang=zh)
                result = subprocess.run(
                    ['fc-list', ':lang=zh', 'file'],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0 and result.stdout:
                    # Parse the first returned font file
                    fonts = result.stdout.strip().split('\n')
                    for line in fonts:
                        file_path = line.split(':')[0].strip()
                        if file_path.lower().endswith(('.ttf', '.ttc', '.otf')):
                            if os.path.exists(file_path):
                                return file_path
            except Exception:
                pass # fc-list not available or failed
                
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
                
        # 4. Final desperate fallback -> try to just load by generic name (often works on Windows)
        for name in ["msyh.ttc", "simhei.ttf", "NotoSansCJK-Regular.ttc"]:
            try:
                # Tests if Pillow can find it in OS specific ways
                ImageFont.truetype(name, 10)
                return name
            except IOError:
                pass
                
        print("[XGP Plugin WARNING] Absolutely no Chinese font could be found on your system!")
        return None

    def _get_font(self, size):
        if self.font_path:
            try:
                return ImageFont.truetype(self.font_path, size)
            except IOError:
                pass
        return ImageFont.load_default()

    async def _download_image(self, url: str) -> Image.Image:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=20.0)
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGBA")
        except Exception:
            return None

    def _crop_to_ratio(self, img: Image.Image, target_ratio: float) -> Image.Image:
        """Crop image to match exactly target_width/target_height ratio from the center"""
        w, h = img.size
        current_ratio = w / h
        
        if current_ratio > target_ratio:
            # Too wide, crop width
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            return img.crop((left, 0, left + new_w, h))
        elif current_ratio < target_ratio:
            # Too tall, crop height
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            return img.crop((0, top, w, top + new_h))
        return img

    def _add_rounded_corners(self, img: Image.Image, radius: int) -> Image.Image:
        """Add rounded corners to an image"""
        mask = Image.new('L', img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
        
        result = img.copy()
        if result.mode != 'RGBA':
            result = result.convert('RGBA')
        result.putalpha(mask)
        return result

    def _draw_text_with_shadow(self, draw: ImageDraw.ImageDraw, xy, text: str, font, fill, shadow_fill="black", shadow_offset=(2,2)):
        x, y = xy
        # Draw shadow
        draw.text((x + shadow_offset[0], y + shadow_offset[1]), text, font=font, fill=shadow_fill)
        # Draw actual text
        draw.text(xy, text, font=font, fill=fill)

    async def generate_announcement_image(self, title: str, games: list[dict]) -> bytes:
        """
        Generate a horizontally laid out announcement image.
        games is a list of dicts:
        {
            "title": "...",
            "image_url": "...",
            "platforms": "Xbox Series X|S · PC",
            "has_zh": True
        }
        """
        # Calculate image width based on number of games, up to a max per row
        num_games = len(games)
        if num_games == 0:
             return b""
             
        # Standard poster size inside the layout
        poster_w, poster_h = 300, 450
        spacing = 80
        padding_top = 320
        padding_bottom = 420
        padding_side = 120
        
        # Max 6 items per row for visual clarity, perfectly fitting most official drops
        items_per_row = min(num_games, 6)
        num_rows = math.ceil(num_games / items_per_row)
        
        img_w = padding_side * 2 + items_per_row * poster_w + (items_per_row - 1) * spacing
        img_h = padding_top + padding_bottom + num_rows * poster_h + (num_rows - 1) * 220
        
        # Create base image with gradient (top current green, bottom deeper green)
        base_img = self._draw_vertical_gradient(int(img_w), int(img_h), XGP_GREEN, "#084d08")
        draw = ImageDraw.Draw(base_img)
        
        # Draw main title centered
        title_bbox = draw.textbbox((0, 0), title, font=self.font_title)
        title_w = title_bbox[2] - title_bbox[0]
        title_x = (img_w - title_w) // 2
        
        # Draw bold title using stroke_width
        draw.text((title_x, 90), title, font=self.font_title, fill=WHITE, stroke_width=2)
        
        # Fetch all images concurrently
        async def fetch_poster(game):
            url = game.get("image_url")
            if url:
                # Add a retry mechanism
                for _ in range(2):
                    img = await self._download_image(url)
                    if img: return img
            return None
            
        posters_raw = await asyncio.gather(*(fetch_poster(g) for g in games))
        
        # Draw games
        for idx, game in enumerate(games):
            row = idx // items_per_row
            col = idx % items_per_row
            
            x = padding_side + col * (poster_w + spacing)
            y = padding_top + row * (poster_h + 220)
            
            # Process poster
            raw_img = posters_raw[idx]
            if raw_img:
                poster = self._crop_to_ratio(raw_img, 2/3)
                poster = poster.resize((poster_w, poster_h), Image.Resampling.LANCZOS)
            else:
                # Fallback placeholder
                poster = Image.new("RGBA", (poster_w, poster_h), "#333333")
                d = ImageDraw.Draw(poster)
                v_box = d.textbbox((0,0), "加载失败", font=self.font_game_title)
                d.text(((poster_w - (v_box[2]-v_box[0]))//2, (poster_h - (v_box[3]-v_box[1]))//2), "无封面", font=self.font_game_title, fill=WHITE)
            
            # Add rounded corners to poster
            poster = self._add_rounded_corners(poster, 16)
            
            # Draw a thin white border behind the poster
            draw.rounded_rectangle([x - 2, y - 2, x + poster_w + 2, y + poster_h + 2], radius=18, fill=WHITE)
            base_img.paste(poster, (x, y), mask=poster)
            # Draw "支持中文" badge overlaid on the bottom right of the poster
            if game.get("has_zh"):
                badge_text = "支持中文"
                badge_bbox = draw.textbbox((0, 0), badge_text, font=self.font_badge)
                bw, bh = badge_bbox[2] - badge_bbox[0], badge_bbox[3] - badge_bbox[1]
                
                # Position it perfectly on the bottom right inside the poster
                # Create a parallelogram banner
                banner_padding_h = 24
                banner_padding_v = 10
                banner_h = bh + banner_padding_v * 2
                banner_w = bw + banner_padding_h * 2
                
                # Coordinates for the parallelogram in the bottom right corner
                p_x1 = x + poster_w - banner_w
                p_x2 = x + poster_w
                p_y1 = y + poster_h - banner_h - 10 # slightly up from bottom
                p_y2 = y + poster_h - 10
                
                # Left side is slanted (parallelogram effect)
                slant = 20
                poly = [
                    (p_x1 + slant, p_y1), # Top-left
                    (p_x2, p_y1),         # Top-right
                    (p_x2, p_y2),         # Bottom-right
                    (p_x1, p_y2)          # Bottom-left
                ]
                
                draw.polygon(poly, fill=BADGE_GREEN)
                
                # Center text in top-down using 'mm' anchor for precision
                text_center_x = p_x1 + slant + (banner_w - slant) / 2
                text_center_y = p_y1 + banner_h / 2
                draw.text((text_center_x, text_center_y), badge_text, font=self.font_badge, fill=WHITE, stroke_width=1, anchor="mm")
                
            # Draw game title below poster (pixel-perfect truncation)
            g_title = game["title"]
            max_width = poster_w
            if draw.textlength(g_title, font=self.font_game_title) > max_width:
                while len(g_title) > 0 and draw.textlength(g_title + "...", font=self.font_game_title) > max_width:
                    g_title = g_title[:-1]
                g_title += "..."
            draw.text((x, y + poster_h + 30), g_title, font=self.font_game_title, fill=WHITE, stroke_width=1)
            
            # 4. Draw subscription tier (ULTIMATE, PREMIUM)
            tier_text = game.get("tier", "ULTIMATE").upper()
            
            # Truncate tier text if too long
            if self.font_tier.getlength(tier_text) > poster_w:
                while self.font_tier.getlength(tier_text + "...") > poster_w and len(tier_text) > 0:
                    tier_text = tier_text[:-1]
                tier_text += "..."
                
            tier_y = y + poster_h + 85
            draw.text((x, tier_y), tier_text, font=self.font_tier, fill=WHITE)
            
            # 5. Draw platforms (smaller, below tier)
            platform_text = game.get("platforms", "主机")
            # Replace "主机" with "主 机" for a premium spaced look as requested before
            platform_text = platform_text.replace("主机", "主 机")
            
            # Truncate platform text if too long
            if draw.textlength(platform_text, font=self.font_platform) > poster_w:
                while draw.textlength(platform_text + "...", font=self.font_platform) > poster_w and len(platform_text) > 0:
                    platform_text = platform_text[:-1]
                platform_text += "..."

            platform_y = tier_y + 35
            draw.text((x, platform_y), platform_text, font=self.font_platform, fill=LIGHT_GREEN, stroke_width=1)

        # Draw bottom right logo badge
        badge_w, badge_h = 220, 176
        bx = img_w - padding_side - badge_w + 40
        by = img_h - padding_bottom + 200
        
        # White rounded rectangle
        draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=24, fill=WHITE)
        
        # Draw Authentic Xbox Sphere Geometry or User Custom Logo
        cx, cy = bx + badge_w / 2, by + 48
        if self.xbox_icon:
            # The icon is black (#1a1a1a) based on our generation script
            icon_w, icon_h = self.xbox_icon.size
            base_img.paste(self.xbox_icon, (int(cx - icon_w/2), int(cy - icon_h/2)), self.xbox_icon)
        else:
            r = 30
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#1a1a1a")
            # Top gap
            draw.chord([cx-r-8, cy-r-40, cx+r+8, cy+r-20], 50, 130, fill=WHITE)
            # Bottom gap
            draw.chord([cx-r-8, cy-r+20, cx+r+8, cy+r+40], 230, 310, fill=WHITE)
            # Side gaps
            offset = 20
            draw.chord([cx-r-40, cy-r-10, cx+r-offset, cy+r+10], 320, 40, fill=WHITE)
            draw.chord([cx-r+offset, cy-r-10, cx+r+40, cy+r+10], 140, 220, fill=WHITE)
        
        # Xbox Game Pass text (centered)
        xbox_w = draw.textlength("Xbox", font=self.font_logo)
        draw.text((cx - xbox_w/2, by + 88), "Xbox", font=self.font_logo, fill="#1a1a1a", stroke_width=1)
        
        gp_text = "Game Pass"
        gp_w = draw.textlength(gp_text, font=self.font_logo)
        draw.text((cx - gp_w/2, by + 123), gp_text, font=self.font_logo, fill="#1a1a1a", stroke_width=1)
        
        # Footer credit line
        footer = "Xbox Game Pass 入库提醒 · by xiaoruange39 · Powered by AstrBot"
        footer_w = draw.textlength(footer, font=self.font_footer)
        draw.text(((img_w - footer_w) / 2, img_h - 40), footer, font=self.font_footer, fill="#88cc88")
        
        # Save to BytesIO, using JPEG to drastically cut down file size and upload time
        output = io.BytesIO()
        base_img.convert("RGB").save(output, format="JPEG", quality=85)
        output.seek(0)
        return output.getvalue()
