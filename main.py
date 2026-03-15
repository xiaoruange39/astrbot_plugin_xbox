import os
import json
import asyncio
import httpx
import tempfile
from datetime import datetime
from croniter import croniter
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from .image_utils import XGPImageGenerator

# Game Pass Catalog API List IDs
XGP_RECENTLY_ADDED = "f13cf6b4-57e6-4459-89df-6aec18cf0538"
XGP_ALL_PC_GAMES = "fdd9e2a7-0fee-49f6-ad69-4354098401ff"
XGP_ALL_CONSOLE_GAMES = "f6f1f99f-9b49-4ccd-b3bf-4d9767a77f5e"

@register("astrbot_plugin_xbox", "xiaoruange39", "Xbox Game Pass 入库提醒与查询插件", "1.0.0")
class XGPNotifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info(f"XGP 插件已加载，配置项数量: {len(self.config)}")
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_xbox"))
        self.known_games_path = os.path.join(self.data_dir, "known_games.json")
        self.discovery_path = os.path.join(self.data_dir, "new_discovery.json")
        self.last_pushed_path = os.path.join(self.data_dir, "last_pushed_games.json")
        
        # Load persisted data
        self.known_games_list: list[str] = self._load_json_list(self.known_games_path)
        self.known_games_set: set[str] = set(self.known_games_list)
        self.new_discovery: list[str] = self._load_json_list(self.discovery_path)
        self.last_pushed_games: list[str] = self._load_json_list(self.last_pushed_path)
        
        self.client = httpx.AsyncClient(timeout=15.0)
        self.check_interval_seconds = 1800  # 30 minutes for background discovery
        self.image_gen = XGPImageGenerator()
        self._state_lock = asyncio.Lock()
        
        # Start background polling task (for discovery and update tracking)
        self.poll_task = asyncio.create_task(self._background_check())
        # Start cron polling task (for notifications)
        self.cron_task = asyncio.create_task(self._cron_loop())
        
        logger.info("XGP 入库通知插件初始化成功，后台任务已启动。")

    def _load_json_list(self, path: str) -> list[str]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [item for item in data if isinstance(item, str)]
                logger.warning(f"Expected list in {path}, got {type(data).__name__}")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load {path}: {e}")
        return []

    def _save_json_list(self, path: str, data: list[str]):
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"Failed to save {path}: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    async def _fetch_gamepass_lists(self, list_ids: list[str], market: str = "US") -> list[str]:
        """批量获取多个 Game Pass 列表中的去重游戏 ID（含基本重试）"""
        all_ids = []
        for lid in list_ids:
            url = f"https://catalog.gamepass.com/sigls/v2?id={lid}&market={market}&language=en-US"
            last_err = None
            for attempt in range(3):
                try:
                    resp = await self.client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            ids = [item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]
                            all_ids.extend(ids)
                        last_err = None
                        break
                    elif resp.status_code >= 500:
                        last_err = f"HTTP {resp.status_code}"
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        last_err = f"HTTP {resp.status_code}"
                        break  # Client errors are not retryable
                except httpx.TimeoutException as e:
                    last_err = e
                    await asyncio.sleep(2 ** attempt)
                except httpx.HTTPError as e:
                    last_err = e
                    break
                except Exception as e:
                    last_err = e
                    break
            if last_err:
                logger.error(f"Failed to fetch list {lid} (mkt={market}) after retries: {last_err}")

        seen = set()
        unique_ids = []
        for gid in all_ids:
            if gid and gid not in seen:
                unique_ids.append(gid)
                seen.add(gid)
        return unique_ids

    def _parse_product(self, product: dict, pc_ids: set | None = None, console_ids: set | None = None) -> dict | None:
        """Parse a single product from the catalog API response."""
        pid = product.get("ProductId", "unknown")
        try:
            lp_list = product.get("LocalizedProperties") or []
            if not lp_list:
                return None
            lp = lp_list[0]
            
            title = lp.get("ProductTitle") or lp.get("ShortTitle") or "Unknown"
            desc = lp.get("ProductDescription") or lp.get("ShortDescription") or ""
            publisher = lp.get("PublisherName") or "Unknown"
            
            # Extract poster image URL
            image_url = ""
            images = lp.get("Images") or []
            for props in images:
                if props and props.get("ImagePurpose") == "Poster":
                    img_uri = props.get("Uri")
                    if img_uri:
                        image_url = img_uri if img_uri.startswith("http") else "https:" + img_uri
                        break
            if not image_url and images:
                first_img = images[0]
                if first_img and isinstance(first_img, dict):
                    img_uri = first_img.get("Uri")
                    if img_uri:
                        image_url = img_uri if img_uri.startswith("http") else "https:" + img_uri
            
            # Detect Chinese language support
            has_zh = self._detect_chinese_support(product)
            
            # Extract subscription tiers
            tiers = self._extract_tiers(product)
            tier_str = " · ".join(tiers) if tiers else "ULTIMATE"
            
            # Determine platform tags
            platform_str = self._determine_platforms(product, pid, pc_ids, console_ids)

            return {
                "id": pid,
                "title": title,
                "description": desc,
                "publisher": publisher,
                "image_url": image_url,
                "has_zh": has_zh,
                "platforms": platform_str,
                "tier": tier_str
            }
        except Exception as e:
            logger.error(f"Error parsing product {pid}: {e}")
            return None

    def _detect_chinese_support(self, product: dict) -> bool:
        """Check if a product supports Chinese language."""
        # 1. Check top-level MarketProperties
        market_props = product.get("MarketProperties") or []
        for mp in market_props:
            supported_langs = mp.get("SupportedLanguages") or []
            if any("zh" in str(lang).lower() for lang in supported_langs):
                return True
        
        # 2. Check DisplaySkuAvailabilities
        sku_avails = product.get("DisplaySkuAvailabilities") or []
        for sa in sku_avails:
            sku = sa.get("Sku") or {}
            s_market_props = sku.get("MarketProperties") or []
            for smp in s_market_props:
                s_langs = smp.get("SupportedLanguages") or []
                if any("zh" in str(lang).lower() for lang in s_langs):
                    return True
        return False

    def _extract_tiers(self, product: dict) -> list[str]:
        """Extract subscription tier information from a product."""
        tiers = []
        for loc_prop in (product.get("LocalizedProperties") or []):
            elig = loc_prop.get("EligibilityProperties")
            if elig:
                affirmations = elig.get("Affirmations") or []
                for aff in affirmations:
                    desc_text = aff.get("Description", "")
                    if "Ultimate" in desc_text and "ULTIMATE" not in tiers:
                        tiers.append("ULTIMATE")
                    if "Premium" in desc_text and "PREMIUM" not in tiers:
                        tiers.append("PREMIUM")
        return tiers

    def _determine_platforms(self, product: dict, pid: str, pc_ids: set | None = None, console_ids: set | None = None) -> str:
        """Determine platform tags for a product."""
        p_tags = []
        is_pc = (pc_ids is not None and pid in pc_ids)
        is_xbox = (console_ids is not None and pid in console_ids)
        
        xbox_props = (product.get("Properties") or {})
        xbox_gen = xbox_props.get("XboxConsoleGenOptimized") or []
        has_gen9 = "ConsoleGen9" in xbox_gen
        
        if is_xbox:
            if has_gen9 and "ConsoleGen8" not in xbox_gen:
                p_tags.append("Xbox Series X|S")
            else:
                p_tags.append("主机")
        
        if is_pc:
            p_tags.append("PC")
            
        return " · ".join(p_tags) if p_tags else "主机 · PC"

    async def _fetch_game_details(self, game_ids: list[str], pc_ids: set | None = None, console_ids: set | None = None) -> list[dict]:
        """Fetch detailed information for a list of game IDs (concurrent batches)."""
        if not game_ids:
            return []

        sem = asyncio.Semaphore(3)

        async def fetch_batch(batch: list[str]) -> list[dict]:
            ids_str = ",".join(batch)
            url = f"https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds={ids_str}&market=CN&languages=zh-CN,en-US"
            async with sem:
                try:
                    resp = await self.client.get(url)
                    if resp.status_code != 200:
                        return []
                    data = resp.json()
                    results = []
                    for product in data.get("Products", []):
                        parsed = self._parse_product(product, pc_ids, console_ids)
                        if parsed:
                            results.append(parsed)
                    return results
                except Exception as e:
                    logger.error(f"Failed to fetch batch {ids_str}: {e}")
                    return []

        batches = [game_ids[i:i+20] for i in range(0, len(game_ids), 20)]
        batch_results = await asyncio.gather(*(fetch_batch(b) for b in batches))
        details = []
        for batch_list in batch_results:
            details.extend(batch_list)
        return details

    async def _background_check(self):
        """后台轮询发现新游戏并维护库纯净度"""
        await asyncio.sleep(10)
        while True:
            try:
                markets = ["US", "HK", "CN"]
                official_new_lists = [
                    XGP_RECENTLY_ADDED,
                    "ced24fc9-d18e-4c6b-8af8-3600ca459424",  # New on PC
                    "4942cf41-9492-4113-ac0c-25089304323c"   # New on Console
                ]

                all_current_ids_ordered = []
                all_current_ids_seen = set()
                official_recent_ordered = []

                for mkt in markets:
                    m_pc = await self._fetch_gamepass_lists([XGP_ALL_PC_GAMES], mkt)
                    m_console = await self._fetch_gamepass_lists([XGP_ALL_CONSOLE_GAMES], mkt)
                    m_new = await self._fetch_gamepass_lists(official_new_lists, mkt)

                    for gid in m_pc:
                        if gid not in all_current_ids_seen:
                            all_current_ids_ordered.append(gid)
                            all_current_ids_seen.add(gid)
                    for gid in m_console:
                        if gid not in all_current_ids_seen:
                            all_current_ids_ordered.append(gid)
                            all_current_ids_seen.add(gid)

                    for nid in m_new:
                        if nid not in official_recent_ordered:
                            official_recent_ordered.append(nid)

                # Guard: only update state if we actually got data from APIs
                if not all_current_ids_seen:
                    logger.warning("Background check got empty results from all markets, skipping state update.")
                    await asyncio.sleep(self.check_interval_seconds)
                    continue

                new_ids = [gid for gid in all_current_ids_ordered if gid not in self.known_games_set]

                async with self._state_lock:
                    # Remove games that left the library (safe: all_current_ids_seen is non-empty)
                    self.new_discovery = [gid for gid in self.new_discovery if gid in all_current_ids_seen]

                    if not self.known_games_list:
                        # Initial baseline
                        self.known_games_list = list(all_current_ids_ordered)
                        self.known_games_set = set(all_current_ids_ordered)
                        self._save_json_list(self.known_games_path, self.known_games_list)
                        self.new_discovery = official_recent_ordered[:50]
                    elif new_ids:
                        logger.info(f"Background check found {len(new_ids)} new shadow-drops.")
                        self.known_games_list = list(new_ids) + self.known_games_list
                        if len(self.known_games_list) > 5000:
                            self.known_games_list = self.known_games_list[:5000]
                        self.known_games_set = set(self.known_games_list)
                        self._save_json_list(self.known_games_path, self.known_games_list)

                        for gid in reversed(new_ids):
                            if gid not in self.new_discovery:
                                self.new_discovery.insert(0, gid)
                        self.new_discovery = self.new_discovery[:200]

                    # Merge official new ranking
                    discovery_set = set(self.new_discovery)
                    for gid in official_recent_ordered:
                        if gid not in discovery_set:
                            self.new_discovery.append(gid)
                            discovery_set.add(gid)
                    self.new_discovery = self.new_discovery[:200]
                    self._save_json_list(self.discovery_path, self.new_discovery)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in background check: {e}")
            await asyncio.sleep(self.check_interval_seconds)

    async def _cron_loop(self):
        """定时推送任务循环"""
        while True:
            cron_expr = self.config.get("cron_time", "").strip()
            if not cron_expr:
                await asyncio.sleep(60)
                continue

            try:
                cron_iter = croniter(cron_expr, datetime.now())
                next_time = cron_iter.get_next(datetime)
                wait_seconds = (next_time - datetime.now()).total_seconds()

                if wait_seconds > 0:
                    logger.info(f"XGP 定时任务下次执行时间: {next_time}")
                    # Sleep in short intervals to detect config changes
                    while wait_seconds > 0:
                        sleep_chunk = min(wait_seconds, 30)
                        await asyncio.sleep(sleep_chunk)
                        wait_seconds -= sleep_chunk
                        # Re-check config: if cron_time changed or cleared, break
                        new_cron = self.config.get("cron_time", "").strip()
                        if new_cron != cron_expr:
                            logger.info("Cron 配置已变更，重新计算下次执行时间。")
                            break
                    else:
                        # Loop completed without break -> execute push
                        await self._perform_scheduled_push()
                        continue
                    # Broke out of while -> config changed, restart outer loop
                    continue

                await self._perform_scheduled_push()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cron loop error: {e}")
                await asyncio.sleep(60)

    async def _fetch_all_library_ids(self) -> tuple[set[str], set[str], set[str]]:
        """Fetch PC + Console IDs across all tracked markets for accurate filtering.

        Returns:
            (all_ids, us_pc_ids, us_console_ids)
        """
        all_ids: set[str] = set()
        us_pc_ids: set[str] = set()
        us_console_ids: set[str] = set()
        for mkt in ["US", "HK", "CN"]:
            pc = await self._fetch_gamepass_lists([XGP_ALL_PC_GAMES], mkt)
            console = await self._fetch_gamepass_lists([XGP_ALL_CONSOLE_GAMES], mkt)
            all_ids.update(pc)
            all_ids.update(console)
            if mkt == "US":
                us_pc_ids = set(pc)
                us_console_ids = set(console)
        return all_ids, us_pc_ids, us_console_ids

    async def _perform_scheduled_push(self):
        """执行定时推送逻辑"""
        targets = self.config.get("push_targets", [])
        if not targets:
            logger.info("未配置推送目标，跳过定时推送。")
            return

        push_on_update_only = self.config.get("push_on_update_only", True)
        if push_on_update_only:
            if set(self.new_discovery) == set(self.last_pushed_games):
                logger.info("Game Pass 数据未更新，跳过定时推送。")
                return

        logger.info(f"开始执行 XGP 定时推送，目标数: {len(targets)}")

        temp_path = None
        try:
            all_lib, pc_ids, console_ids = await self._fetch_all_library_ids()

            async with self._state_lock:
                discovery_snapshot = list(self.new_discovery)
            target_ids = [gid for gid in discovery_snapshot if gid in all_lib]

            if not target_ids:
                self.last_pushed_games = discovery_snapshot
                self._save_json_list(self.last_pushed_path, self.last_pushed_games)
                return

            limit = min(self.config.get("display_limit", 10), 36)
            details = await self._fetch_game_details(target_ids[:limit], pc_ids, console_ids)
            if not details:
                self.last_pushed_games = discovery_snapshot
                self._save_json_list(self.last_pushed_path, self.last_pushed_games)
                return

            img_bytes = await self.image_gen.generate_announcement_image("现已加入 Xbox Game Pass", details)
            if img_bytes:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(img_bytes)
                    temp_path = tmp.name

                chain = MessageChain().file_image(temp_path)
                for umo in targets:
                    try:
                        await self.context.send_message(umo, chain)
                    except Exception as e:
                        logger.error(f"Failed to push to {umo}: {e}")

                self.last_pushed_games = discovery_snapshot
                self._save_json_list(self.last_pushed_path, self.last_pushed_games)
        except Exception as e:
            logger.error(f"Scheduled push failed: {e}")
        finally:
            if temp_path:
                await asyncio.sleep(5)
                try:
                    os.remove(temp_path)
                except OSError as e:
                    logger.debug(f"Failed to remove temp file {temp_path}: {e}")

    @filter.command("xgp")
    async def xgp(self, event: AstrMessageEvent):
        '''查看最近入库的 Game Pass 游戏'''
        async for result in self._handle_xgp_query(event):
            yield result

    async def _handle_xgp_query(self, event: AstrMessageEvent):
        '''查询 Game Pass 入库信息的核心逻辑'''
        if self.config.get("show_loading_msg", True):
            yield event.plain_result("正在获取Xbox Game Pass 入库信息，请稍后...")

        temp_path = None
        try:
            all_lib, pc_ids, console_ids = await self._fetch_all_library_ids()

            async with self._state_lock:
                discovery_snapshot = list(self.new_discovery)
            target_ids = [gid for gid in discovery_snapshot if gid in all_lib]
            if not target_ids:
                yield event.plain_result("😅 暂未发现新入库的游戏。")
                return

            limit = min(self.config.get("display_limit", 10), 36)
            details = await self._fetch_game_details(target_ids[:limit], pc_ids, console_ids)

            if not details:
                yield event.plain_result("😅 无法获取游戏详情，请稍后再试。")
                return

            img_bytes = await self.image_gen.generate_announcement_image("现已加入 Xbox Game Pass", details)
            if img_bytes:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(img_bytes)
                    temp_path = tmp.name
                yield event.image_result(temp_path)
            else:
                yield event.plain_result(f"获取图片失败，共找到 {len(details)} 个游戏。")
        except Exception as e:
            logger.error(f"xgp query failed: {e}")
            yield event.plain_result(f"❌ 运行失败: {e}")
        finally:
            if temp_path:
                await asyncio.sleep(2)
                try:
                    os.remove(temp_path)
                except OSError as e:
                    logger.debug(f"Failed to remove temp file {temp_path}: {e}")

    async def terminate(self):
        '''插件卸载时关闭后台任务。'''
        tasks_to_cancel = []
        if self.poll_task and not self.poll_task.done():
            self.poll_task.cancel()
            tasks_to_cancel.append(self.poll_task)
        if self.cron_task and not self.cron_task.done():
            self.cron_task.cancel()
            tasks_to_cancel.append(self.cron_task)
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        await self.client.aclose()
        await self.image_gen.close()
        logger.info("XGP 插件已卸载，清理完毕。")
