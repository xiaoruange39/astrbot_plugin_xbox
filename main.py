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
        
        # Start background polling task (for discovery and update tracking)
        self.poll_task = asyncio.create_task(self._background_check())
        # Start cron polling task (for notifications)
        self.cron_task = asyncio.create_task(self._cron_loop())
        
        logger.info("XGP 入库通知插件初始化成功，后台任务已启动。")

    def _load_json_list(self, path: str) -> list[str]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load {path}: {e}")
        return []

    def _save_json_list(self, path: str, data: list[str]):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {path}: {e}")

    async def _fetch_gamepass_lists(self, list_ids: list[str], market: str = "US") -> list[str]:
        """批量获取多个 Game Pass 列表中的去重游戏 ID"""
        all_ids = []
        for lid in list_ids:
            url = f"https://catalog.gamepass.com/sigls/v2?id={lid}&market={market}&language=en-US"
            try:
                resp = await self.client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        ids = [item["id"] for item in data if isinstance(item, dict) and "id" in item]
                        all_ids.extend(ids)
            except Exception as e:
                logger.error(f"Failed to fetch list {lid} (mkt={market}): {e}")
        
        seen = set()
        unique_ids = []
        for gid in all_ids:
            if gid and gid not in seen:
                unique_ids.append(gid)
                seen.add(gid)
        return unique_ids

    def _parse_product(self, product: dict, pc_ids: set = None, console_ids: set = None) -> dict | None:
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
                        image_url = "https:" + img_uri
                        break
            if not image_url and images:
                first_img = images[0]
                if first_img and isinstance(first_img, dict):
                    img_uri = first_img.get("Uri")
                    if img_uri:
                        image_url = "https:" + img_uri
            
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

    def _determine_platforms(self, product: dict, pid: str, pc_ids: set = None, console_ids: set = None) -> str:
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

    async def _fetch_game_details(self, game_ids: list[str], pc_ids: set = None, console_ids: set = None) -> list[dict]:
        """Fetch detailed information for a list of game IDs."""
        if not game_ids:
            return []
            
        details = []
        for i in range(0, len(game_ids), 20):
            batch = game_ids[i:i+20]
            ids_str = ",".join(batch)
            url = f"https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds={ids_str}&market=CN&languages=zh-CN,en-US"
            
            try:
                resp = await self.client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                products = data.get("Products", [])
                for product in products:
                    parsed = self._parse_product(product, pc_ids, console_ids)
                    if parsed:
                        details.append(parsed)
            except Exception as e:
                logger.error(f"Failed to fetch batch {ids_str}: {e}")
        return details

    async def _background_check(self):
        """后台轮询发现新游戏并维护库纯净度"""
        await asyncio.sleep(10)
        while True:
            try:
                markets = ["US", "HK", "CN"]
                official_new_lists = [
                    XGP_RECENTLY_ADDED,
                    "ced24fc9-d18e-4c6b-8af8-3600ca459424", # New on PC
                    "4942cf41-9492-4113-ac0c-25089304323c"  # New on Console
                ]
                
                all_current_ids_ordered = []
                all_current_ids_seen = set()
                official_recent_ordered = []
                
                # Fetch baseline libraries
                pc_ids = set(await self._fetch_gamepass_lists([XGP_ALL_PC_GAMES], "US"))
                console_ids = set(await self._fetch_gamepass_lists([XGP_ALL_CONSOLE_GAMES], "US"))
                
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

                new_ids = [gid for gid in all_current_ids_ordered if gid not in self.known_games_set]
                
                # Update discovery storage (remove games that left)
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
                    if len(self.known_games_list) > 1000:
                        self.known_games_list = self.known_games_list[:1000]
                    self.known_games_set = set(self.known_games_list)
                    self._save_json_list(self.known_games_path, self.known_games_list)
                    
                    # Prepend discovered IDs to discovery list
                    for gid in reversed(new_ids):
                        if gid not in self.new_discovery:
                            self.new_discovery.insert(0, gid)
                    self.new_discovery = self.new_discovery[:200]
                
                # Merge official new ranking into discovery list for comprehensive tracking
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
                    await asyncio.sleep(wait_seconds)
                
                await self._perform_scheduled_push()
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cron loop error: {e}")
                await asyncio.sleep(60)

    async def _perform_scheduled_push(self):
        """执行定时推送逻辑"""
        targets = self.config.get("push_targets", [])
        if not targets:
            logger.info("未配置推送目标，跳过定时推送。")
            return

        # Check for updates if configured
        push_on_update_only = self.config.get("push_on_update_only", True)
        if push_on_update_only:
            # Compare current discovery with last pushed
            if set(self.new_discovery) == set(self.last_pushed_games):
                logger.info("Game Pass 数据未更新，跳过定时推送。")
                return

        logger.info(f"开始执行 XGP 定时推送，目标数: {len(targets)}")
        
        # We need a dummy event-like context for handle_catalog_command logic
        # But we can just call it directly to get the image
        try:
            # Re-fetch everything to ensure accuracy
            pc_ids = await self._fetch_gamepass_lists([XGP_ALL_PC_GAMES], "US")
            console_ids = await self._fetch_gamepass_lists([XGP_ALL_CONSOLE_GAMES], "US")
            all_lib = set(pc_ids) | set(console_ids)
            
            # Use current discovery list filtered by absolute library membership
            target_ids = [gid for gid in self.new_discovery if gid in all_lib]
            
            if not target_ids:
                return

            limit = self.config.get("display_limit", 10)
            details = await self._fetch_game_details(target_ids[:limit], set(pc_ids), set(console_ids))
            if not details:
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
                
                # Update last pushed cache
                self.last_pushed_games = list(self.new_discovery)
                self._save_json_list(self.last_pushed_path, self.last_pushed_games)
                
                await asyncio.sleep(5)
                try:
                    os.remove(temp_path)
                except OSError as e:
                    logger.debug(f"Failed to remove temp file {temp_path}: {e}")
        except Exception as e:
            logger.error(f"Scheduled push failed: {e}")

    @filter.command("xgp")
    async def xgp(self, event: AstrMessageEvent):
        '''查看最近入库的 Game Pass 游戏'''
        async for result in self._handle_xgp_query(event):
            yield result

    @filter.regex(r"^xgp$")
    async def xgp_no_prefix(self, event: AstrMessageEvent):
        '''无前缀触发 xgp 查询'''
        if not self.config.get("no_prefix_trigger", False):
            return
        async for result in self._handle_xgp_query(event):
            yield result

    async def _handle_xgp_query(self, event: AstrMessageEvent):
        '''查询 Game Pass 入库信息的核心逻辑'''
        if self.config.get("show_loading_msg", True):
            yield event.plain_result("正在获取Xbox Game Pass 入库信息，请稍后...")
        
        try:
            pc_ids = await self._fetch_gamepass_lists([XGP_ALL_PC_GAMES], "US")
            console_ids = await self._fetch_gamepass_lists([XGP_ALL_CONSOLE_GAMES], "US")
            all_lib = set(pc_ids) | set(console_ids)
            
            target_ids = [gid for gid in self.new_discovery if gid in all_lib]
            if not target_ids:
                yield event.plain_result("😅 暂未发现新入库的游戏。")
                return

            limit = self.config.get("display_limit", 10)
            details = await self._fetch_game_details(target_ids[:limit], set(pc_ids), set(console_ids))
            
            if not details:
                yield event.plain_result("😅 无法获取游戏详情，请稍后再试。")
                return

            img_bytes = await self.image_gen.generate_announcement_image("现已加入 Xbox Game Pass", details)
            if img_bytes:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(img_bytes)
                    temp_path = tmp.name
                yield event.image_result(temp_path)
                await asyncio.sleep(2)
                try:
                    os.remove(temp_path)
                except OSError as e:
                    logger.debug(f"Failed to remove temp file {temp_path}: {e}")
            else:
                yield event.plain_result(f"获取图片失败，共找到 {len(details)} 个游戏。")
        except Exception as e:
            logger.error(f"xgp query failed: {e}")
            yield event.plain_result(f"❌ 运行失败: {e}")

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
