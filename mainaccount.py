import requests
import asyncio
import nest_asyncio
import logging
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Configuration ===
TELEGRAM_BOT_TOKEN = "7507216734:AAENmzI_ZMruoiBJ3vd8DDI6MxLmD0iBqvA"
TELEGRAM_CHAT_ID = "-1002510646965"
WALLET_ADDRESS = "0xadd5647a27987d3b5447cea68e2aaa56e9b522f3"
HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"

nest_asyncio.apply()  # Fix for asyncio loop issues

class HyperLiquidBot:
    def __init__(self):
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.mark_price_cache = {}
        self.cache_expiry = 0
        self.cache_duration = 10  # Cache duration in seconds

    async def get_mark_prices(self):
        """Fetch current mark prices for all coins with caching"""
        current_time = time.time()
        if current_time < self.cache_expiry:
            return self.mark_price_cache
            
        try:
            payload = {"type": "allMids"}
            response = requests.post(
                HYPERLIQUID_API,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5
            )
            response.raise_for_status()
            self.mark_price_cache = response.json()
            self.cache_expiry = current_time + self.cache_duration
            return self.mark_price_cache
        except Exception as e:
            logger.error(f"Error fetching mark prices: {e}")
            return {}

    async def get_hyperliquid_positions(self):
        """Fetch positions with accurate current prices"""
        try:
            # Get current mark prices first
            mark_prices = await self.get_mark_prices()
            
            # Get wallet positions
            payload = {"type": "clearinghouseState", "user": WALLET_ADDRESS}
            response = requests.post(
                HYPERLIQUID_API,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            positions = []
            for pos in data.get('assetPositions', []):
                try:
                    position = pos.get('position', {})
                    if not position or not isinstance(position, dict):
                        continue

                    coin = position.get('coin')
                    if not coin:
                        continue

                    # Helper function for safe number conversion
                    def to_float(value, default=0.0):
                        try:
                            if isinstance(value, (dict, list)):
                                return float(value[0] if isinstance(value, list) else value.get('value', default))
                            return float(value)
                        except (ValueError, TypeError):
                            return default

                    # FIXED: More robust size extraction
                    size_str = position.get('szi')
                    size = to_float(size_str) if size_str is not None else 0
                    if size == 0:  # Skip zero positions
                        continue

                    entry_price = to_float(position.get('entryPx'))
                    
                    # Get current price - priority: mark_prices > position.markPx > entry_price
                    current_price = to_float(mark_prices.get(coin), entry_price)
                    if current_price == entry_price:  # Fallback if mark_prices didn't have it
                        current_price = to_float(position.get('markPx'), entry_price)

                    # Calculate position values
                    position_value_usd = abs(size) * current_price
                    entry_value_usd = abs(size) * entry_price
                    leverage = to_float(position.get('leverage'), 1.0)
                    margin_used = position_value_usd / leverage if leverage != 0 else 0

                    positions.append({
                        'symbol': coin,
                        'size': size,
                        'entry': entry_price,
                        'current': current_price,
                        'position_value_usd': position_value_usd,
                        'entry_value_usd': entry_value_usd,
                        'leverage': leverage,
                        'margin_used': margin_used,
                        'liq_price': to_float(position.get('liquidationPx')),
                        'pnl': to_float(position.get('unrealizedPnl')),
                        'timestamp': int(time.time())
                    })
                except Exception as e:
                    logger.error(f"Error processing position {pos}: {e}")

            return positions
        except Exception as e:
            logger.error(f"API Error: {e}")
            return None

    async def get_spot_holdings(self):
        """Fetch spot holdings with USDC always at $1 and unified token fallback"""
        try:
            payload = {"type": "spotClearinghouseState", "user": WALLET_ADDRESS}
            response = requests.post(
                HYPERLIQUID_API,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            mark_prices = await self.get_mark_prices()
            holdings = []
            
            for balance in data.get('balances', []):
                try:
                    coin = balance.get('coin')
                    total = float(balance.get('total', 0))
                    entry_ntl = float(balance.get('entryNtl', 0))  # Entry value in USD

                    # Special handling for USDC
                    if coin == "USDC":
                        current_price = 1.0
                        entry_price = 1.0
                        unrealized_pnl = 0.0  # Force PnL to 0 for USDC
                        roe = 0.0
                    else:
                        # Handle unified tokens (UBTC -> BTC, UETH -> ETH, UFART -> FARTCOIN)
                        unified_mapping = {
                            'UBTC': 'BTC',
                            'UETH': 'ETH',
                            'USOL': 'SOL',
                            'UFART': 'FARTCOIN'
                        }
                        base_coin = unified_mapping.get(coin, coin)
                        current_price = float(mark_prices.get(coin, 0))
                        if current_price == 0 and coin in unified_mapping:
                            current_price = float(mark_prices.get(base_coin, 0))
                        
                        entry_price = entry_ntl / total if total > 0 else 0
                        unrealized_pnl = (total * current_price) - entry_ntl
                        roe = (unrealized_pnl / entry_ntl * 100) if entry_ntl != 0 else 0

                    current_value = total * current_price

                    holdings.append({
                        'coin': coin,
                        'total': total,
                        'entry_price': entry_price,
                        'current_price': current_price,
                        'entry_ntl': entry_ntl,
                        'current_value': current_value,
                        'unrealized_pnl': unrealized_pnl,
                        'roe': roe,
                        'timestamp': int(time.time())
                    })
                except Exception as e:
                    logger.error(f"Error processing balance {balance}: {e}")

            return holdings
        except Exception as e:
            logger.error(f"Error fetching spot holdings: {e}")
            return None

    async def get_withdrawable_balance(self):
        """Fetch withdrawable balance from HyperLiquid (correct implementation)"""
        try:
            payload = {
                "type": "clearinghouseState",
                "user": WALLET_ADDRESS
            }
            
            response = requests.post(
                HYPERLIQUID_API,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            # Extract the cross margin summary
            cross_margin_summary = data.get("crossMarginSummary", {})
            
            # The withdrawable balance is the accountValue in cross margin summary
            withdrawable = float(cross_margin_summary.get("accountValue", 0))
            
            return withdrawable
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching withdrawable balance: {e}")
            return None
        except ValueError as e:
            logger.error(f"Error parsing withdrawable balance response: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching withdrawable balance: {e}")
            return None

    async def format_positions(self, positions):
        """Format positions into human-readable message with portfolio metrics"""
        if not positions:
            return "ℹ️ No open positions found"

        # Calculate portfolio metrics
        total_entry_value = sum(pos['entry_value_usd'] for pos in positions)
        total_unrealized_pnl = sum(pos['pnl'] for pos in positions)
        Current_position_size = total_entry_value + total_unrealized_pnl
        total_margin_used = sum(pos['margin_used'] for pos in positions)
        margin_usage_pct = (total_margin_used / Current_position_size) * 100 if Current_position_size > 0 else 0
        
        long_pnl = sum(pos['pnl'] for pos in positions if pos['size'] > 0)
        short_pnl = sum(pos['pnl'] for pos in positions if pos['size'] < 0)

        message = [
            f"📊 *Portfolio Overview - {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}*",
            f"Last updated: <t:{positions[0]['timestamp']}:R>",
            "",
            f"• Current Position Size: ${Current_position_size:,.2f}",
            f"• Margin Usage: ${total_margin_used:,.2f} ({margin_usage_pct:.1f}%)",
            f"• Long PnL: ${long_pnl:+,.2f}",
            f"• Short PnL: ${short_pnl:+,.2f}",
            f"• Unrealized PnL: ${total_unrealized_pnl:+,.2f}",
            "",
            "🔹 *Open Positions*",
            ""
        ]

        for idx, pos in enumerate(positions, 1):
            direction = "LONG" if pos['size'] > 0 else "SHORT"
            pnl_emoji = "🟢" if pos['pnl'] >= 0 else "🔴"
            
            # Calculate PnL percentage
            pnl_percent = (pos['pnl'] / pos['entry_value_usd']) * 100 if pos['entry_value_usd'] != 0 else 0
            
            # Calculate price change percentage
            price_change_pct = ((pos['current'] - pos['entry']) / pos['entry']) * 100

            message.extend([
                f"*{idx}. {pos['symbol']}* ({direction})",
                f"Size: {abs(pos['size']):.4f} (${pos['position_value_usd']:,.2f})",
                f"Entry: ${pos['entry']:.2f} (${pos['entry_value_usd']:,.2f})",
                f"Current: ${pos['current']:.2f} ({price_change_pct:+.2f}%)",
                f"Leverage: {pos['leverage']:.1f}x",
                f"Liq Price: ${pos['liq_price']:.2f}",
                f"PnL: {pnl_emoji} ${pos['pnl']:+,.2f} ({pnl_percent:+.2f}%)",
                ""
            ])
        
        return "\n".join(message)

    async def format_spot_holdings(self, holdings):
        """Format spot holdings into human-readable message"""
        if not holdings:
            return "ℹ️ No spot holdings found"

        total_current = sum(h['current_value'] for h in holdings)
        total_pnl = sum(h['unrealized_pnl'] for h in holdings)

        message = [
            f"💰 *Spot Holdings - {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}*",
            f"Last updated: <t:{holdings[0]['timestamp']}:R>",
            "",
            f"• Total Value: ${total_current:,.2f}",
            f"• Unrealized PnL: ${total_pnl:+,.2f}",
            "",
            "🔹 *Detailed Holdings*",
            "",
            f"`{'Coin':<6} {'Balance':<12} {'Entry':<8} {'Current':<8} {'Value (USD)':<12} {'PnL':<12} {'ROE%':<8}`"
        ]

        for h in holdings:
            pnl_emoji = "🟢" if h['unrealized_pnl'] >= 0 else "🔴"
            message.append(
                f"`{h['coin']:<6} {h['total']:<12.4f} {h['entry_price']:<8.2f} {h['current_price']:<8.2f} "
                f"{h['current_value']:<12.2f} {pnl_emoji} {h['unrealized_pnl']:<+12.2f} {h['roe']:<+8.2f}`"
            )

        return "\n".join(message)

    async def format_asset_summary(self, positions, holdings, withdrawable):
        """Format asset summary into human-readable message"""
        # Calculate perps account metrics
        perps_entry_value = sum(pos['entry_value_usd'] for pos in positions) if positions else 0
        perps_pnl = sum(pos['pnl'] for pos in positions) if positions else 0
        perps_current_value = perps_entry_value + perps_pnl
        perps_margin_used = sum(pos['margin_used'] for pos in positions) if positions else 0
        perps_margin_pct = (perps_margin_used / perps_current_value * 100) if perps_current_value > 0 else 0
        
        # Calculate Total Perps Equity
        total_perps_equity = (withdrawable + perps_margin_used + perps_pnl)

        # Calculate spot holdings metrics
        spot_total = sum(h['current_value'] for h in holdings) if holdings else 0
        spot_non_usdc = sum(h['current_value'] for h in holdings if h['coin'] != 'USDC') if holdings else 0
        spot_usdc = sum(h['current_value'] for h in holdings if h['coin'] == 'USDC') if holdings else 0
        spot_pnl = sum(h['unrealized_pnl'] for h in holdings) if holdings else 0

        # Calculate total equity
        total_equity = total_perps_equity + spot_total

        # Calculate spot percentage
        spot_pct = (spot_non_usdc / (perps_current_value + spot_non_usdc)) * 100 if (perps_current_value + spot_non_usdc) > 0 else 0

        message = [
            f"📈 *Asset Summary - {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]}*",
            f"Last updated: <t:{int(time.time())}:R>",
            "",
            f"• Total Equity: ${total_equity:,.2f}",
            "",
            "🔹 *Perps Account*",
            f"• Total Perps Equity: ${total_perps_equity:,.2f}",
            f"• Open Trades Size: ${perps_current_value:,.2f}",
            f"• Margin Usage: {perps_margin_pct:.1f}% (${perps_margin_used:,.2f})",
            f"• Unrealized PnL: ${perps_pnl:+,.2f}",
            f"🔹 Withdrawable: ${withdrawable:,.2f}" if withdrawable is not None else "• Withdrawable: Failed to fetch",
            "",
            "🔹 *Spot Holdings*",
            f"• Spot Equity: ${spot_total:,.2f}",
            f"• Holdings: ${spot_non_usdc:,.2f} ({spot_pct:.1f}% of total Portfolio)",
            f"• USDC: ${spot_usdc:,.2f}",
            f"• Spot PnL: ${spot_pnl:+,.2f}"
        ]

        return "\n".join(message)

    async def open_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler for /open_trades command"""
        if str(update.effective_chat.id) != TELEGRAM_CHAT_ID:
            await update.message.reply_text("❌ Unauthorized access.")
            return

        msg = await update.message.reply_text("🔄 Fetching positions...")
        positions = await self.get_hyperliquid_positions()

        if positions is None:
            await msg.edit_text("⚠️ Failed to fetch data. Please try again later.")
            return

        response = await self.format_positions(positions)
        await msg.edit_text(response, parse_mode="Markdown")

    async def spot_holdings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler for /spot command"""
        if str(update.effective_chat.id) != TELEGRAM_CHAT_ID:
            await update.message.reply_text("❌ Unauthorized access.")
            return

        msg = await update.message.reply_text("🔄 Fetching spot holdings...")
        holdings = await self.get_spot_holdings()

        if holdings is None:
            await msg.edit_text("⚠️ Failed to fetch spot data. Please try again later.")
            return

        response = await self.format_spot_holdings(holdings)
        await msg.edit_text(response, parse_mode="Markdown")

    async def asset_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler for /asset command"""
        if str(update.effective_chat.id) != TELEGRAM_CHAT_ID:
            await update.message.reply_text("❌ Unauthorized access.")
            return

        msg = await update.message.reply_text("🔄 Fetching asset summary...")
        
        # Fetch all required data
        positions = await self.get_hyperliquid_positions()
        holdings = await self.get_spot_holdings()
        withdrawable = await self.get_withdrawable_balance()

        if positions is None or holdings is None:
            await msg.edit_text("⚠️ Failed to fetch some data. Please try again later.")
            return

        response = await self.format_asset_summary(positions, holdings, withdrawable)
        await msg.edit_text(response, parse_mode="Markdown")

    def run_bot(self):
        """Start the bot with all commands"""
        self.application.add_handler(CommandHandler("open_trades", self.open_trades))
        self.application.add_handler(CommandHandler("spot", self.spot_holdings))
        self.application.add_handler(CommandHandler("asset", self.asset_summary))
        logger.info("Bot is running. Commands: /open_trades, /spot, /asset")
        self.application.run_polling()

if __name__ == "__main__":
    try:
        bot = HyperLiquidBot()
        bot.run_bot()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")