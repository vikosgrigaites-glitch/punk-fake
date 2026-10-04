import os
import logging
import time
import json
import asyncio
import hashlib
import hmac
import base64

import httpx
import aiosqlite

from threading import Thread
from flask import Flask
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from solders.hash import Hash


# ==============================================================================
# APLINKOS KINTAMIEJI
# ==============================================================================

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

DB_PATH = '/var/data/database.db' if os.path.exists('/var/data') else 'database.db'


# ==============================================================================
# FLASK
# ==============================================================================

app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is alive!"


# ==============================================================================
# NUSTATYMAI
# ==============================================================================

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

MAIN_WALLET_ADDRESS = os.getenv('MAIN_WALLET_ADDRESS')
MASTER_SEED_STRING = os.getenv('MASTER_SEED_STRING')
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY')

if not TELEGRAM_TOKEN:
    logging.warning("TELEGRAM_TOKEN nenustatytas!")

if not MASTER_SEED_STRING:
    logging.warning("MASTER_SEED_STRING nenustatytas!")

if not HELIUS_API_KEY:
    logging.warning("HELIUS_API_KEY nenustatytas!")

SOLANA_RPC_URL = (
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
)

RESERVATION_TIME_SECONDS = 45 * 60
BASKET_RESERVATION_TIME = 45 * 60

http_client = httpx.AsyncClient(timeout=10.0)

cached_sol_price = 130.0
last_price_fetch_time = 0


# ==============================================================================
# SOLANA RPC
# ==============================================================================

class SimpleSolanaClient:

    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def get_balance(self, pubkey: Pubkey):

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(pubkey)]
        }

        try:
            response = await http_client.post(
                self.endpoint,
                json=payload
            )

            res_json = response.json()

            if (
                "result" in res_json
                and res_json["result"] is not None
            ):
                val = res_json["result"].get("value", 0)

                class ResultObj:
                    def __init__(self, v):
                        self.value = v

                return ResultObj(val)

        except Exception as e:
            logging.error(f"RPC klaida (getBalance): {e}")

        class ResultObj:
            def __init__(self, v):
                self.value = v

        return ResultObj(None)

    async def get_latest_blockhash(self):

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getLatestBlockhash",
            "params": [
                {
                    "commitment": "finalized"
                }
            ]
        }

        try:
            response = await http_client.post(
                self.endpoint,
                json=payload
            )

            res_json = response.json()

            if (
                "result" in res_json
                and "value" in res_json["result"]
            ):
                return res_json["result"]["value"]["blockhash"]

        except Exception as e:
            logging.error(
                f"RPC klaida (getLatestBlockhash): {e}"
            )

        return None

    async def send_transaction(self, tx: Transaction):

        tx_bytes = bytes(tx)
        tx_b64 = base64.b64encode(tx_bytes).decode('utf-8')

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_b64,
                {
                    "encoding": "base64"
                }
            ]
        }

        try:
            response = await http_client.post(
                self.endpoint,
                json=payload
            )

            return response.json()

        except Exception as e:
            logging.error(
                f"RPC klaida (sendTransaction): {e}"
            )

        return None


solana_client = SimpleSolanaClient(SOLANA_RPC_URL)


# ==============================================================================
# SOL KAINA
# ==============================================================================

async def get_sol_price_in_eur() -> float:

    global cached_sol_price
    global last_price_fetch_time

    now = time.time()

    if now - last_price_fetch_time > 120:

        try:

            url = (
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=solana&vs_currencies=eur"
            )

            response = await http_client.get(url)

            if response.status_code == 200:

                res = response.json()

                if (
                    "solana" in res
                    and "eur" in res["solana"]
                ):
                    cached_sol_price = float(
                        res["solana"]["eur"]
                    )

                    last_price_fetch_time = now

        except Exception as e:
            logging.error(
                f"Klaida gaunant SOL kursą: {e}"
            )

    return cached_sol_price


# ==============================================================================
# DATABASE
# ==============================================================================

async def init_db():

    async with aiosqlite.connect(DB_PATH) as conn:

        await conn.execute(
            'PRAGMA journal_mode=WAL;'
        )

        await conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                spent REAL DEFAULT 0.0,
                status TEXT DEFAULT 'Naujas',
                basket TEXT DEFAULT '[]',
                history TEXT DEFAULT '[]'
            )
            '''
        )

        await conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS products (
                city_id TEXT,
                city_name TEXT,
                product_id TEXT,
                product_name TEXT,
                price REAL,
                category TEXT DEFAULT 'Bendras',
                PRIMARY KEY (city_id, product_id)
            )
            '''
        )

        await conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS product_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id TEXT,
                product_id TEXT,
                photo_id TEXT
            )
            '''
        )

        await conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS active_orders (
                order_index INTEGER PRIMARY KEY,
                user_id INTEGER,
                order_data TEXT
            )
            '''
        )

        await conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            '''
        )

        async with conn.execute(
            "PRAGMA table_info(products)"
        ) as cursor:

            columns = [
                column[1]
                for column in await cursor.fetchall()
            ]

            if 'category' not in columns:

                await conn.execute(
                    "ALTER TABLE products "
                    "ADD COLUMN category TEXT DEFAULT 'Bendras'"
                )

        await conn.commit()


async def get_all_users():

    async with aiosqlite.connect(DB_PATH) as conn:

        async with conn.execute(
            '''
            SELECT
                user_id,
                balance,
                spent,
                status,
                history
            FROM users
            '''
        ) as cursor:

            return await cursor.fetchall()


# ==============================================================================
# BENDRAS WALLET SKAITIKLIS
# ==============================================================================

async def get_next_wallet_counter() -> int:

    async with aiosqlite.connect(DB_PATH) as conn:

        await conn.execute("BEGIN IMMEDIATE")

        async with conn.execute(
            """
            SELECT value
            FROM settings
            WHERE key = 'wallet_seq'
            """
        ) as cursor:

            row = await cursor.fetchone()

        if row is None:

            next_index = 1

            await conn.execute(
                """
                INSERT INTO settings (key, value)
                VALUES ('wallet_seq', ?)
                """,
                (str(next_index),)
            )

        else:

            try:
                current_index = int(row[0])
            except Exception:
                current_index = 0

            next_index = current_index + 1

            await conn.execute(
                """
                UPDATE settings
                SET value = ?
                WHERE key = 'wallet_seq'
                """,
                (str(next_index),)
            )

        await conn.commit()

    address = derive_solana_address(next_index)

    logging.info(
        f"Naujas SOL wallet: "
        f"index={next_index}, address={address}"
    )

    return next_index


# ==============================================================================
# ACTIVE ORDERS
# ==============================================================================

async def save_active_order(
    order_index: int,
    user_id: int,
    order_dict: dict
):

    async with aiosqlite.connect(DB_PATH) as conn:

        await conn.execute(
            """
            INSERT OR REPLACE INTO active_orders
            (
                order_index,
                user_id,
                order_data
            )
            VALUES (?, ?, ?)
            """,
            (
                order_index,
                user_id,
                json.dumps(order_dict)
            )
        )

        await conn.commit()


async def delete_active_order(order_index: int):

    async with aiosqlite.connect(DB_PATH) as conn:

        await conn.execute(
            """
            DELETE FROM active_orders
            WHERE order_index = ?
            """,
            (order_index,)
        )

        await conn.commit()


async def delete_user_active_orders(user_id: int):

    async with aiosqlite.connect(DB_PATH) as conn:

        await conn.execute(
            """
            DELETE FROM active_orders
            WHERE user_id = ?
            """,
            (user_id,)
        )

        await conn.commit()


async def get_all_active_orders() -> dict:

    async with aiosqlite.connect(DB_PATH) as conn:

        async with conn.execute(
            """
            SELECT
                order_index,
                user_id,
                order_data
            FROM active_orders
            """
        ) as cursor:

            rows = await cursor.fetchall()

    orders = {}

    for o_idx, uid, o_json in rows:

        try:

            data = json.loads(o_json)
            data["user_id"] = uid
            orders[o_idx] = data

        except Exception:

            pass

    return orders


# ==============================================================================
# SOLANA WALLET DERIVATION
# ==============================================================================

def derive_solana_keypair(index: int) -> Keypair:

    if not MASTER_SEED_STRING:

        raise RuntimeError(
            "MASTER_SEED_STRING nenustatytas .env faile!"
        )

    if index < 1:

        raise ValueError(
            "Wallet index turi būti >= 1"
        )

    master_key = MASTER_SEED_STRING.encode(
        "utf-8"
    )

    message = (
        f"solana-deposit-wallet:{index}"
    ).encode("utf-8")

    seed_32 = hmac.new(
        master_key,
        message,
        hashlib.sha256
    ).digest()

    return Keypair.from_seed(seed_32)


def derive_solana_address(index: int) -> str:

    kp = derive_solana_keypair(index)

    return str(kp.pubkey())


def derive_legacy_solana_keypair(index: int) -> Keypair:

    if not MASTER_SEED_STRING:

        raise RuntimeError(
            "MASTER_SEED_STRING nenustatytas!"
        )

    combined_seed = (
        f"{MASTER_SEED_STRING}_{index}"
    ).encode("utf-8")

    seed_32 = (
        combined_seed.ljust(32, b'0')[:32]
    )

    return Keypair.from_seed(seed_32)


def derive_legacy_solana_address(index: int) -> str:

    kp = derive_legacy_solana_keypair(index)

    return str(kp.pubkey())


def get_order_wallet_index(order_index, order):

    wallet_index = order.get("wallet_index")

    if wallet_index is not None:

        try:
            return int(wallet_index)
        except Exception:
            pass

    return None


def get_order_address(order_index, order):

    address = order.get("address")

    if address:
        return address

    wallet_index = get_order_wallet_index(
        order_index,
        order
    )

    if wallet_index is not None:

        return derive_solana_address(
            wallet_index
        )

    return derive_legacy_solana_address(
        order_index
    )


# ==============================================================================
# SOL BALANCE
# ==============================================================================

async def check_address_balance_raw(
    address_str: str
) -> float:

    try:

        pubkey = Pubkey.from_string(
            address_str
        )

        balance_res = await solana_client.get_balance(
            pubkey
        )

        if balance_res.value is not None:

            return balance_res.value / 1_000_000_000

    except Exception as e:

        logging.error(
            f"Klaida tikrinant balansą: {e}"
        )

    return 0.0


# ==============================================================================
# SOL SWEEP
# ==============================================================================

async def send_sol_to_main_wallet(
    wallet_index: int
) -> bool:

    try:

        if (
            not MAIN_WALLET_ADDRESS
            or MAIN_WALLET_ADDRESS.startswith("ČIA_")
        ):

            logging.error(
                "MAIN_WALLET_ADDRESS nenustatytas!"
            )

            return False

        from_keypair = derive_solana_keypair(
            wallet_index
        )

        to_pubkey = Pubkey.from_string(
            MAIN_WALLET_ADDRESS
        )

        balance_res = await solana_client.get_balance(
            from_keypair.pubkey()
        )

        FEE_RESERVE = 10_000

        if balance_res.value is None:

            logging.error(
                f"Nepavyko gauti wallet "
                f"#{wallet_index} balanso."
            )

            return False

        if balance_res.value <= FEE_RESERVE:

            logging.info(
                f"Wallet #{wallet_index} "
                f"neturi pakankamai SOL sweep'ui."
            )

            return False

        lamports_to_send = (
            balance_res.value - FEE_RESERVE
        )

        blockhash_str = (
            await solana_client.get_latest_blockhash()
        )

        if not blockhash_str:

            logging.error(
                f"Nepavyko gauti blockhash "
                f"wallet #{wallet_index}."
            )

            return False

        recent_blockhash = Hash.from_string(
            blockhash_str
        )

        transfer_ix = transfer(
            TransferParams(
                from_pubkey=from_keypair.pubkey(),
                to_pubkey=to_pubkey,
                lamports=lamports_to_send
            )
        )

        tx = Transaction.new_signed_with_payer(
            [transfer_ix],
            from_keypair.pubkey(),
            [from_keypair],
            recent_blockhash
        )

        res = await solana_client.send_transaction(
            tx
        )

        if res and "result" in res:

            signature = res["result"]

            logging.info(
                f"Sėkmingas sweep: "
                f"wallet #{wallet_index} -> "
                f"{MAIN_WALLET_ADDRESS}, "
                f"signature={signature}"
            )

            return True

        logging.error(
            f"Sweep nepavyko wallet "
            f"#{wallet_index}: {res}"
        )

        return False

    except Exception as e:

        logging.exception(
            f"Klaida siunčiant SOL iš "
            f"wallet #{wallet_index}: {e}"
        )

        return False


# ==============================================================================
# CATALOG
# ==============================================================================

async def get_catalog_from_db():

    async with aiosqlite.connect(DB_PATH) as conn:

        async with conn.execute(
            """
            SELECT
                city_id,
                city_name,
                product_id,
                product_name,
                price,
                category
            FROM products
            """
        ) as cursor:

            rows = await cursor.fetchall()

        catalog = {}

        for (
            city_id,
            city_name,
            prod_id,
            prod_name,
            price,
            category
        ) in rows:

            if city_id not in catalog:

                catalog[city_id] = {
                    "name": city_name,
                    "products": []
                }

            async with conn.execute(
                """
                SELECT photo_id
                FROM product_photos
                WHERE city_id = ?
                AND product_id = ?
                """,
                (
                    city_id,
                    prod_id
                )
            ) as cursor_photos:

                photos = [
                    p[0]
                    for p in await cursor_photos.fetchall()
                ]

            catalog[city_id]["products"].append(
                {
                    "id": prod_id,
                    "name": prod_name,
                    "price": price,
                    "category": (
                        category
                        if category
                        else "Bendras"
                    ),
                    "photos": photos
                }
            )

    return catalog


async def pop_product_photo(
    city_id: str,
    prod_id: str
):

    async with aiosqlite.connect(DB_PATH) as conn:

        async with conn.execute(
            """
            SELECT id, photo_id
            FROM product_photos
            WHERE city_id = ?
            AND product_id = ?
            LIMIT 1
            """,
            (
                city_id,
                prod_id
            )
        ) as cursor:

            row = await cursor.fetchone()

        photo_id = None
        remaining_count = 0

        if row:

            photo_db_id, photo_id = row

            await conn.execute(
                """
                DELETE FROM product_photos
                WHERE id = ?
                """,
                (photo_db_id,)
            )

            await conn.commit()

            async with conn.execute(
                """
                SELECT COUNT(*)
                FROM product_photos
                WHERE city_id = ?
                AND product_id = ?
                """,
                (
                    city_id,
                    prod_id
                )
            ) as cursor_count:

                count_row = await cursor_count.fetchone()

                remaining_count = (
                    count_row[0]
                    if count_row
                    else 0
                )

        return photo_id, remaining_count


# ==============================================================================
# USER DATA
# ==============================================================================

async def get_user_data(user_id: int):

    async with aiosqlite.connect(DB_PATH) as conn:

        async with conn.execute(
            """
            SELECT
                balance,
                spent,
                status,
                basket,
                history
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:

            row = await cursor.fetchone()

    if not row:

        default_basket = json.dumps([])
        default_history = json.dumps([])

        async with aiosqlite.connect(DB_PATH) as conn_insert:

            await conn_insert.execute(
                """
                INSERT INTO users
                (
                    user_id,
                    balance,
                    spent,
                    status,
                    basket,
                    history
                )
                VALUES (?, 0.0, 0.0, 'Naujas', ?, ?)
                """,
                (
                    user_id,
                    default_basket,
                    default_history
                )
            )

            await conn_insert.commit()

        return {
            "balance": 0.0,
            "spent": 0.0,
            "status": "Naujas",
            "basket": [],
            "history": []
        }

    try:

        basket_data = (
            json.loads(row[3])
            if isinstance(row[3], str)
            else row[3]
        )

    except Exception:

        basket_data = []

    try:

        history_data = (
            json.loads(row[4])
            if isinstance(row[4], str)
            else row[4]
        )

    except Exception:

        history_data = []

    return {
        "balance": row[0],
        "spent": row[1],
        "status": row[2],
        "basket": (
            basket_data
            if isinstance(basket_data, list)
            else []
        ),
        "history": (
            history_data
            if isinstance(history_data, list)
            else []
        )
    }


async def save_user_data(
    user_id: int,
    u_data: dict
):

    async with aiosqlite.connect(DB_PATH) as conn:

        await conn.execute(
            """
            UPDATE users
            SET
                balance = ?,
                spent = ?,
                status = ?,
                basket = ?,
                history = ?
            WHERE user_id = ?
            """,
            (
                u_data["balance"],
                u_data["spent"],
                u_data["status"],
                json.dumps(u_data["basket"]),
                json.dumps(u_data["history"]),
                user_id
            )
        )

        await conn.commit()


# ==============================================================================
# BASKET & RESERVATIONS
# ==============================================================================

async def clean_expired_basket(user_id: int):

    u_data = await get_user_data(user_id)
    current_time = time.time()
    valid_items = []
    changed = False

    basket_list = u_data.get('basket', [])

    if isinstance(basket_list, str):
        try:
            basket_list = json.loads(basket_list)
        except Exception:
            basket_list = []
            changed = True

    if isinstance(basket_list, list):
        for item in basket_list:
            if isinstance(item, dict) and 'added_at' in item:
                if (current_time - item['added_at']) < BASKET_RESERVATION_TIME:
                    valid_items.append(item)
                else:
                    changed = True
            else:
                changed = True

    if changed or len(valid_items) != len(u_data.get('basket', [])):
        u_data['basket'] = valid_items
        await save_user_data(user_id, u_data)


async def get_reserved_count_in_baskets(
    city_id: str,
    prod_id: str
) -> int:
    current_time = time.time()
    reserved_count = 0

    # 1. Tikrinam vartotojų krepšelius
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT basket FROM users") as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        if not row[0]:
            continue
        try:
            basket = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if isinstance(basket, list):
                for item in basket:
                    if (
                        isinstance(item, dict)
                        and item.get("city_id") == city_id
                        and item.get("opt_id") == prod_id
                    ):
                        if (current_time - item.get("added_at", 0)) < BASKET_RESERVATION_TIME:
                            reserved_count += 1
        except Exception:
            pass

    # 2. Tikrinam aktyvius (nepabaigtus apmokėti) SOL užsakymus, kad prekė iškart nusirezervuotų paspaudus "Pirkti iškart"
    active_orders = await get_all_active_orders()
    for o_idx, order in active_orders.items():
        if (current_time - order.get("created_at", 0)) < RESERVATION_TIME_SECONDS:
            if order.get("type") == "single":
                if order.get("city_id") == city_id and order.get("opt_id") == prod_id:
                    reserved_count += 1
            elif order.get("type") == "basket":
                for b_item in order.get("items", []):
                    if b_item.get("city_id") == city_id and b_item.get("opt_id") == prod_id:
                        reserved_count += 1

    return reserved_count


# ==============================================================================
# STOCK NOTIFICATION
# ==============================================================================

async def notify_stock_empty(
    context: ContextTypes.DEFAULT_TYPE,
    item_name: str,
    city_id: str
):

    try:

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "⚠️ **DĖMESIO: NUOTRAUKŲ LIKUTIS PASIBAIGĖ!**\n\n"
                f"Prekės **{item_name}** ({city_id}) "
                "nuotraukų likutis pasiekė **0 vnt.**!"
            ),
            parse_mode='Markdown'
        )

    except Exception as e:

        logging.error(
            f"Klaida siunčiant pranešimą: {e}"
        )


# ==============================================================================
# AUTOMATINIS MOKĖJIMŲ TIKRINIMAS
# ==============================================================================

async def auto_check_payments(
    context: ContextTypes.DEFAULT_TYPE
):

    now = time.time()
    sol_price_eur = await get_sol_price_in_eur()
    active_orders = await get_all_active_orders()

    for order_idx, order in list(active_orders.items()):

        try:

            elapsed = now - order["created_at"]
            user_id = order["user_id"]

            if elapsed > RESERVATION_TIME_SECONDS:

                await delete_active_order(order_idx)

                try:
                    keyboard = InlineKeyboardMarkup(
                        [[
                            InlineKeyboardButton(
                                "🏠 Grįžti į pagrindinį meniu",
                                callback_data='home'
                            )
                        ]]
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "⏰ **Mokėjimo laikas "
                            "(45 min.) pasibaigė.**\n\n"
                            "Užsakymas buvo atšauktas."
                        ),
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                except Exception:
                    pass

                continue

            address = get_order_address(order_idx, order)
            actual_sol = await check_address_balance_raw(address)

            margin_sol = (
                0.01 / sol_price_eur
                if sol_price_eur > 0
                else 0.001
            )

            if order.get("type") == "topup":

                if actual_sol > 0.00001:

                    credited_eur = actual_sol * sol_price_eur
                    u_data = await get_user_data(user_id)
                    u_data['balance'] += credited_eur
                    await save_user_data(user_id, u_data)

                    wallet_index = order.get("wallet_index")
                    sweep_success = False

                    if wallet_index is not None:
                        sweep_success = await send_sol_to_main_wallet(
                            int(wallet_index)
                        )

                    await delete_active_order(order_idx)

                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "🎉 **Balansas sėkmingai "
                                "papildytas!**\n\n"
                                f"Įskaita: "
                                f"**+{credited_eur:.2f} EUR** "
                                f"({actual_sol:.4f} SOL)"
                            ),
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass

                    try:
                        await context.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                "🔔 **Sėkmingas balanso "
                                "papildymas!**\n\n"
                                f"👤 Vartotojas: `{user_id}`\n"
                                f"💰 Suma: "
                                f"**{actual_sol:.4f} SOL** "
                                f"(~{credited_eur:.2f}€)\n"
                                f"📍 Adresas: `{address}`"
                            ),
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass

                continue

            expected_sol = order.get("price_sol", 0)

            if actual_sol >= (expected_sol - margin_sol):

                u_data = await get_user_data(user_id)
                u_data['spent'] += order['price_eur']

                if order["type"] == "single":

                    unique_photo, remaining = await pop_product_photo(
                        order["city_id"],
                        order["opt_id"]
                    )

                    if unique_photo:

                        u_data['history'].append(
                            {
                                "name": order['item_name'],
                                "price": order['price_eur'],
                                "photo": unique_photo,
                                "date": time.strftime("%Y-%m-%d %H:%M")
                            }
                        )

                        await save_user_data(user_id, u_data)

                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "🎉 **Mokėjimas gautas!**\n\n"
                                f"Dėkojame už pirkimą "
                                f"({order['item_name']})."
                            ),
                            parse_mode='Markdown'
                        )

                        try:
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=unique_photo,
                                caption=(
                                    "📦 Jūsų pirkinys: "
                                    f"{order['item_name']}"
                                )
                            )
                        except Exception:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    "📦 Jūsų pirkinys: "
                                    f"{order['item_name']}\n"
                                    f"Failas/ID: `{unique_photo}`"
                                ),
                                parse_mode='Markdown'
                            )

                        if remaining == 0:
                            await notify_stock_empty(
                                context,
                                order['item_name'],
                                order['city_id']
                            )

                    else:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "🎉 Mokėjimas gautas, "
                                "tačiau šios prekės "
                                "nuotraukų sąrašas tuščias. "
                                "Susisiekite su administratoriumi."
                            )
                        )

                elif order["type"] == "basket":

                    delivered_items = []

                    for item in order["items"]:

                        unique_photo, remaining = await pop_product_photo(
                            item["city_id"],
                            item["opt_id"]
                        )

                        if unique_photo:

                            delivered_items.append(
                                (item["item_name"], unique_photo)
                            )

                            u_data['history'].append(
                                {
                                    "name": item['item_name'],
                                    "price": item['price_eur'],
                                    "photo": unique_photo,
                                    "date": time.strftime("%Y-%m-%d %H:%M")
                                }
                            )

                            if remaining == 0:
                                await notify_stock_empty(
                                    context,
                                    item["item_name"],
                                    item["city_id"]
                                )

                    u_data['basket'] = []
                    await save_user_data(user_id, u_data)

                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "🎉 **Krepšelio mokėjimas gautas!**\n\n"
                            "Siunčiamos jūsų nuotraukos..."
                        ),
                        parse_mode='Markdown'
                    )

                    for item_name, photo_id in delivered_items:

                        try:
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=photo_id,
                                caption=(
                                    f"📦 Jūsų pirkinys: "
                                    f"{item_name}"
                                )
                            )
                        except Exception:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    f"📦 Jūsų pirkinys: "
                                    f"{item_name}\n"
                                    f"ID: `{photo_id}`"
                                ),
                                parse_mode='Markdown'
                            )

                wallet_index = order.get("wallet_index")
                if wallet_index is not None:
                    await send_sol_to_main_wallet(int(wallet_index))

                await delete_active_order(order_idx)

                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "🔔 **Gautas naujas "
                            "mokėjimas už prekę!**\n\n"
                            f"👤 Vartotojas: `{user_id}`\n"
                            f"💰 Suma: "
                            f"**{expected_sol:.6f} SOL** "
                            f"({order['price_eur']:.2f}€)\n"
                            f"📍 Adresas: `{address}`"
                        ),
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logging.error(f"Klaida siunčiant adminui: {e}")

        except Exception as e:
            logging.exception(f"Klaida tikrinant order #{order_idx}: {e}")


# ==============================================================================
# ADMIN PANEL
# ==============================================================================

async def admin_panel_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    text = (
        "⚙️️ **ADMIN VALDYMO SKYDELIS**\n\n"
        "Pasirinkite norimą veiksmą žemiau:"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 Vartotojų valdymas",
                    callback_data='admin_users_list'
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 Likučių apžvalga (/inventory)",
                    callback_data='admin_inv'
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 Aktyvūs SOL adresai ir balansai",
                    callback_data='admin_sol_wallets'
                )
            ],
            [
                InlineKeyboardButton(
                    "📜 Pirkėjų pirkimų istorija",
                    callback_data='admin_buyers_history'
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Skelbti pranešimą visiems",
                    callback_data='admin_broadcast_start'
                )
            ],
            [
                InlineKeyboardButton(
                    "➕ Pridėti naują prekę",
                    callback_data='admin_add_prod_start'
                )
            ],
            [
                InlineKeyboardButton(
                    "📸 Papildyti nuotraukas / likutį",
                    callback_data='admin_add_photo_start'
                )
            ],
            [
                InlineKeyboardButton(
                    "🏷️ Keisti prekės kainą mygtukais",
                    callback_data='admin_edit_price_start'
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Ištrinti prekę",
                    callback_data='admin_del_prod_start'
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Atstatyti vartotojus",
                    callback_data='admin_restart_users_confirm'
                )
            ]
        ]
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )


# ==============================================================================
# USERS MANAGEMENT (ADMIN)
# ==============================================================================

async def show_users_management(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ADMIN_ID:
        return

    users = await get_all_users()
    if not users:
        msg = "👥 **Vartotojų valdymas:**\n\nNėra registruotų vartotojų."
        keyboard = [[InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')]]
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return

    msg = f"👥 **Registruoti vartotojai (Iš viso: {len(users)}):**\nPasirinkite vartotoją redagavimui:\n\n"
    keyboard = []

    for u in users:
        uid, balance, spent, status, history_json = u
        btn_text = f"👤 ID: {uid} | Balansas: {balance:.2f}€"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"adm_usr_mgmt_{uid}")])

    keyboard.append([InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')])
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def show_single_user_mgmt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_uid: int
):
    u_data = await get_user_data(target_uid)
    msg = (
        f"👤 **Vartotojo valdymas**\n\n"
        f"🆔 ID: `{target_uid}`\n"
        f"💵 Balansas: **{u_data['balance']:.2f} EUR**\n"
        f"💰 Išleista: **{u_data['spent']:.2f} EUR**\n"
        f"⭐ Statusas: **{u_data['status']}**\n\n"
        "Pasirinkite veiksmą:"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ Pridėti lėšų (+5€)", callback_data=f"adm_usr_addbal_{target_uid}_5"),
            InlineKeyboardButton("➕ Pridėti lėšų (+10€)", callback_data=f"adm_usr_addbal_{target_uid}_10")
        ],
        [
            InlineKeyboardButton("➖ Atimti lėšas (-5€)", callback_data=f"adm_usr_subbal_{target_uid}_5"),
            InlineKeyboardButton("0️⃣ Anuliuoti balansą", callback_data=f"adm_usr_zerobal_{target_uid}")
        ],
        [
            InlineKeyboardButton("🗑️ Ištrinti vartotoją iš DB", callback_data=f"adm_usr_delete_{target_uid}")
        ],
        [
            InlineKeyboardButton("🔙 Atgal į vartotojų sąrašą", callback_data='admin_users_list')
        ]
    ]

    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# ==============================================================================
# BROADCAST
# ==============================================================================

async def broadcast_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "📢 **Naudojimas:**\n`/broadcast <jūsų žinutė>`",
            parse_mode='Markdown'
        )
        return

    msg_text = " ".join(context.args)
    await send_broadcast_message(context, msg_text)
    await update.message.reply_text("✅ Skelbimas išsiųstas visiems vartotojams!")


async def send_broadcast_message(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    photo_id: str = None
):

    users = await get_all_users()
    success = 0
    failed = 0

    for user in users:
        uid = user[0]
        try:
            if photo_id:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=photo_id,
                    caption=text,
                    parse_mode='Markdown'
                )
            else:
                await context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode='Markdown'
                )
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "📢 **Skelbimo rezultatai:**\n\n"
                f"✅ Sėkmingai pristatyta: **{success}** vartotojų\n"
                f"❌ Nepavyko: **{failed}**"
            ),
            parse_mode='Markdown'
        )
    except Exception:
        pass


# ==============================================================================
# BUYERS HISTORY & INVENTORY
# ==============================================================================

async def show_buyers_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    users = await get_all_users()

    if not users:
        msg = "📜 **Pirkėjų istorija:**\n\nVartotojų duomenų bazė tuščia."
        keyboard = [[InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')]]
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return

    msg = "📜 **Pirkėjų pirkimų istorija:**\nPasirinkite vartotoją:\n\n"
    keyboard = []

    for u in users:
        uid, balance, spent, status, history_json = u
        try:
            history = json.loads(history_json) if isinstance(history_json, str) else history_json
        except Exception:
            history = []

        item_count = len(history) if isinstance(history, list) else 0
        btn_text = f"👤 ID: {uid} | Išleido: {spent:.2f}€ ({item_count} pirkim.)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"adm_usr_hist_{uid}")])

    keyboard.append([InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')])
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def show_user_detail_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_uid: int
):

    u_data = await get_user_data(target_uid)
    history = u_data.get('history', [])

    msg = f"👤 **Vartotojo `{target_uid}` pirkimų istorija:**\n\n"
    msg += f"💵 Balansas: **{u_data['balance']:.2f}€**\n"
    msg += f"💰 Išvis išleido: **{u_data['spent']:.2f}€**\n"
    msg += f"⭐ Statusas: **{u_data['status']}**\n\n"

    keyboard = []

    if not history:
        msg += "_Šis vartotojas dar nieko nepirko._"
    else:
        msg += f"🛒 **Atlikti pirkimai ({len(history)}):**\n"
        for idx, h in enumerate(reversed(history)):
            real_idx = len(history) - 1 - idx
            msg += f"• **{h.get('date', 'N/A')}** | {h.get('name', 'Prekė')} ({h.get('price', 0):.2f}€)\n"
            if h.get('photo'):
                keyboard.append([
                    InlineKeyboardButton(
                        f"🖼️ Gauti nuotrauką #{len(history) - idx} ({h.get('name')})",
                        callback_data=f"adm_get_usr_ph_{target_uid}_{real_idx}"
                    )
                ])

    keyboard.append([InlineKeyboardButton("🔙 Atgal į Pirkėjų Sąrašą", callback_data='admin_buyers_history')])
    keyboard.append([InlineKeyboardButton("⚙️ Admin Skydelis", callback_data='admin_panel')])

    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def inventory_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    catalog = await get_catalog_from_db()

    if not catalog:
        msg = "📦 **Likučių apžvalga:**\n\nParduotuvė kol kas tuščia."
    else:
        msg = "📊 **Likučių ir Nuotraukų apžvalga:**\n\n"
        for city_id, city in catalog.items():
            msg += f"🏙 **{city['name']}** (`{city_id}`):\n"
            if not city.get("products"):
                msg += "  _Nėra prekių_\n"
            for prod in city.get("products", []):
                photos_count = len(prod.get("photos", []))
                reserved = await get_reserved_count_in_baskets(city_id, prod['id'])
                available = photos_count - reserved
                cat = prod.get('category', 'Bendras')
                status_icon = "🟢" if available > 0 else "🔴 (IŠPARDUOTA)"

                msg += f"  • `{prod['id']}` | **{prod['name']}** [{cat}]\n"
                msg += f"    Kaina: **{prod['price']:.2f}€** | Likutis: **{available} vnt.** (Viso: {photos_count}, Rezerv.: {reserved}) {status_icon}\n"
            msg += "\n"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')]])
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=kb, parse_mode='Markdown')
    else:
        await update.message.reply_text(msg, reply_markup=kb, parse_mode='Markdown')


# ==============================================================================
# PRODUCT COMMANDS & SWEEP
# ==============================================================================

async def set_product_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 5:
        await update.message.reply_text("❌ **Naudojimas:**\n`/set_product <miest_id> <prekes_id> <kaina> <kategorija> <pavadinimas>`", parse_mode='Markdown')
        return

    city_id, product_id = args[0].lower(), args[1].lower()
    try:
        price = float(args[2])
    except ValueError:
        await update.message.reply_text("❌ Kaina turi būti skaičius!")
        return

    category, product_name = args[3], " ".join(args[4:])
    city_name = city_id.capitalize()

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            '''
            INSERT INTO products (city_id, city_name, product_id, product_name, price, category)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(city_id, product_id)
            DO UPDATE SET product_name = excluded.product_name, price = excluded.price, category = excluded.category
            ''',
            (city_id, city_name, product_id, product_name, price, category)
        )
        await conn.commit()

    await update.message.reply_text(f"✅ Prekė **{product_name}** išsaugota!", parse_mode='Markdown')


async def delete_product_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ **Naudojimas:**\n`/delete_product <miest_id> <prekes_id>`", parse_mode='Markdown')
        return

    city_id, product_id = args[0].lower(), args[1].lower()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM product_photos WHERE city_id = ? AND product_id = ?", (city_id, product_id))
        await conn.execute("DELETE FROM products WHERE city_id = ? AND product_id = ?", (city_id, product_id))
        await conn.commit()

    await update.message.reply_text(f"❌ Prekė `{product_id}` ištrinta!", parse_mode='Markdown')


async def sweep_all_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    msg = await update.message.reply_text("🔄 Tikrinami aktyvių užsakymų adresai...")
    successful_sweeps = 0
    active_orders = await get_all_active_orders()

    for order_idx, order in active_orders.items():
        try:
            wallet_index = order.get("wallet_index")
            if wallet_index is not None:
                res = await send_sol_to_main_wallet(int(wallet_index))
                if res:
                    successful_sweeps += 1
                await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"Klaida sweep order #{order_idx}: {e}")

    await msg.edit_text(f"✅ Pervesta iš **{successful_sweeps}** aktyvių naujų wallet adresų.", parse_mode='Markdown')


async def restart_users_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE users SET balance = 0.0, spent = 0.0, status = 'Naujas', basket = '[]', history = '[]'")
        await conn.execute("DELETE FROM active_orders")
        await conn.commit()

    msg = "🔄 **Vartotojų duomenys atstatyti!**"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
    else:
        await update.message.reply_text(msg, parse_mode='Markdown')


# ==============================================================================
# USER / ADMIN TEXT / PHOTOS HANDLER
# ==============================================================================

async def handle_admin_text_and_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Vartotojo įvedamos papildymo sumos apdorojimas (Top up)
    if context.user_data.get('awaiting_topup_amount'):
        text = update.message.text.strip().replace(',', '.')
        try:
            amount = float(text)
            if amount < 5.0:
                await update.message.reply_text("❌ Minimali papildymo suma yra **5 EUR**. Įveskite didesnę sumą:", parse_mode='Markdown')
                return

            context.user_data.pop('awaiting_topup_amount', None)
            
            sol_price_eur = await get_sol_price_in_eur()
            if sol_price_eur <= 0:
                await update.message.reply_text("❌ Nepavyko gauti SOL kainos. Bandykite vėliau.")
                return

            required_sol = round(amount / sol_price_eur, 6)
            wallet_index = await get_next_wallet_counter()
            unique_address = derive_solana_address(wallet_index)

            order_dict = {
                "type": "topup",
                "order_index": wallet_index,
                "wallet_index": wallet_index,
                "user_id": user_id,
                "address": unique_address,
                "price_eur": amount,
                "price_sol": required_sol,
                "created_at": time.time()
            }
            await save_active_order(wallet_index, user_id, order_dict)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Atšaukti", callback_data='cancel_confirm')]
            ])
            text_msg = (
                f"💳 **Balanso papildymas (Rezervuota 45 min)**\n\n"
                f"• Suma: **{amount:.2f} EUR**\n"
                f"• Perveskite tiksliai: **{required_sol:.6f} SOL**\n\n"
                f"**Adresas:**\n`{unique_address}`\n\n"
                "🔄 _Pervedimas bus įskaitytas automatiškai._"
            )
            await update.message.reply_text(text_msg, reply_markup=keyboard, parse_mode='Markdown')
            return
        except ValueError:
            await update.message.reply_text("❌ Prašome įvesti teisingą skaičių (min. 5):")
            return

    if user_id != ADMIN_ID:
        return

    if context.user_data.get('awaiting_broadcast'):
        context.user_data.pop('awaiting_broadcast', None)
        text = update.message.caption or update.message.text or ""
        photo_id = update.message.photo[-1].file_id if update.message.photo else None
        await send_broadcast_message(context, text, photo_id)
        return

    if context.user_data.get('awaiting_photo'):
        city_id = context.user_data.get('photo_city_id')
        prod_id = context.user_data.get('photo_prod_id')

        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    "INSERT INTO product_photos (city_id, product_id, photo_id) VALUES (?, ?, ?)",
                    (city_id, prod_id, photo_file_id)
                )
                await conn.commit()
                async with conn.execute(
                    "SELECT COUNT(*) FROM product_photos WHERE city_id = ? AND product_id = ?",
                    (city_id, prod_id)
                ) as cursor:
                    row = await cursor.fetchone()
                    count = row[0] if row else 0

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Siųsti dar vieną", callback_data=f"adm_addph_p_{city_id}_{prod_id}")],
                [InlineKeyboardButton("⚙️ Grįžti", callback_data='admin_panel')]
            ])
            await update.message.reply_text(f"✅ Nuotrauka įkelta! Likutis: **{count} vnt.**", reply_markup=kb, parse_mode='Markdown')
            return

    if update.message.text:
        text = update.message.text.strip()

        if context.user_data.get('awaiting_new_price'):
            try:
                new_price = float(text.replace(',', '.'))
                c_id = context.user_data.pop('edit_city_id')
                p_id = context.user_data.pop('edit_prod_id')
                context.user_data.pop('awaiting_new_price')

                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute("UPDATE products SET price = ? WHERE city_id = ? AND product_id = ?", (new_price, c_id, p_id))
                    await conn.commit()

                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Grįžti", callback_data='admin_panel')]])
                await update.message.reply_text(f"✅ Kaina pakeista į **{new_price:.2f}€**!", reply_markup=kb, parse_mode='Markdown')
                return
            except ValueError:
                await update.message.reply_text("❌ Įveskite skaičių:")
                return

        step = context.user_data.get('add_prod_step')
        if step == 'id':
            context.user_data['new_prod_id'] = text.lower().replace(" ", "_")
            context.user_data['add_prod_step'] = 'name'
            await update.message.reply_text("✏️ Įveskite pavadinimą:", parse_mode='Markdown')
            return
        elif step == 'name':
            context.user_data['new_prod_name'] = text
            context.user_data['add_prod_step'] = 'price'
            await update.message.reply_text("💶 Įveskite kainą eurais:", parse_mode='Markdown')
            return
        elif step == 'price':
            try:
                context.user_data['new_prod_price'] = float(text.replace(',', '.'))
                context.user_data['add_prod_step'] = 'category'
                await update.message.reply_text("🏷️ Įveskite kategoriją:", parse_mode='Markdown')
                return
            except ValueError:
                await update.message.reply_text("❌ Įveskite skaičių:")
                return
        elif step == 'category':
            category = text
            c_id = context.user_data.pop('new_city_id')
            p_id = context.user_data.pop('new_prod_id')
            p_name = context.user_data.pop('new_prod_name')
            p_price = context.user_data.pop('new_prod_price')
            context.user_data.pop('add_prod_step')

            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute(
                    '''
                    INSERT INTO products (city_id, city_name, product_id, product_name, price, category)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(city_id, product_id)
                    DO UPDATE SET product_name = excluded.product_name, price = excluded.price, category = excluded.category
                    ''',
                    (c_id, c_id.capitalize(), p_id, p_name, p_price, category)
                )
                await conn.commit()

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📸 Įkelti nuotraukas", callback_data=f"adm_addph_p_{c_id}_{p_id}")],
                [InlineKeyboardButton("⚙️ Grįžti", callback_data='admin_panel')]
            ])
            await update.message.reply_text("🎉 Prekė sukurta!", reply_markup=kb, parse_mode='Markdown')
            return


# ==============================================================================
# KEYBOARDS & MENUS
# ==============================================================================

def main_menu_keyboard(basket_count: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Parduotuvė", callback_data='menu_shop')],
        [
            InlineKeyboardButton(f"👤 Profilis / Krepšelis ({basket_count})", callback_data='menu_profile'),
            InlineKeyboardButton("💳 Papildyti", callback_data='menu_topup')
        ],
        [
            InlineKeyboardButton("💬 Atsiliepimai", url="https://t.me/punkreviews666"),
            InlineKeyboardButton("🏷 Kainoraštis", callback_data='menu_pricelist')
        ]
    ])


def cities_keyboard(catalog):
    keyboard = [
        [InlineKeyboardButton(f"🏙 {city['name']}", callback_data=f"city_{city_id}")]
        for city_id, city in catalog.items()
    ]
    keyboard.append([InlineKeyboardButton("🏠 Pagrindinis", callback_data='home')])
    return InlineKeyboardMarkup(keyboard)


async def city_products_keyboard(city_id, catalog):
    keyboard = []
    city = catalog.get(city_id, {})

    for prod in city.get("products", []):
        total_photos = len(prod.get("photos", []))
        reserved = await get_reserved_count_in_baskets(city_id, prod['id'])
        available = total_photos - reserved
        cat_tag = f"[{prod.get('category', 'Bendras')}] " if prod.get('category') else ""

        if available <= 0:
            keyboard.append([InlineKeyboardButton(f"❌ {cat_tag}{prod['name']} (Išparduota)", callback_data='sold_out')])
        else:
            keyboard.append([InlineKeyboardButton(f"{cat_tag}{prod['name']} | {prod['price']:.2f}€ ({available} vnt.)", callback_data=f"prod_{city_id}_{prod['id']}")])

    keyboard.append([
        InlineKeyboardButton("🔙 Atgal", callback_data='menu_shop'),
        InlineKeyboardButton("🏠 Pagrindinis", callback_data='home')
    ])
    return InlineKeyboardMarkup(keyboard)


def product_detail_keyboard(city_id, prod_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Pirkti iškart (SOL)", callback_data=f"paynow_{city_id}_{prod_id}")],
        [InlineKeyboardButton("🛒 Įsidėti į krepšelį", callback_data=f"addbasket_{city_id}_{prod_id}")],
        [
            InlineKeyboardButton("🔙 Atgal", callback_data=f"city_{city_id}"),
            InlineKeyboardButton("🏠 Pagrindinis", callback_data='home')
        ]
    ])


def invoice_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Atšaukti", callback_data='cancel_confirm')]])


def profile_basket_keyboard(basket_count: int):
    buttons = []
    if basket_count > 0:
        buttons.append([InlineKeyboardButton("⚡ Mokėti už krepšelį (SOL)", callback_data='pay_basket_sol')])
        buttons.append([InlineKeyboardButton("💳 Mokėti iš balanso", callback_data='pay_basket_balance')])
        buttons.append([InlineKeyboardButton("🗑 Išvalyti krepšelį", callback_data='clear_basket')])
    buttons.append([InlineKeyboardButton("🏠 Pagrindinis", callback_data='home')])
    return InlineKeyboardMarkup(buttons)


async def send_main_home(update: Update):
    user = update.effective_user
    await clean_expired_basket(user.id)
    u_data = await get_user_data(user.id)
    basket_count = len(u_data['basket'])

    text = (
        f"👋 **Sveiki, {user.first_name}!**\n\n"
        f"💵 Balansas: **{u_data['balance']:.2f} EUR**\n"
        f"⭐ Statusas: **{u_data['status']}**\n"
        f"🧺 Krepšelis: **{basket_count} prekė(-s)**\n\n"
        "Pasirinkite norimą veiksmą žemiau:"
    )

    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        await update.callback_query.message.reply_text(text, reply_markup=main_menu_keyboard(basket_count), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(basket_count), parse_mode='Markdown')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_home(update)


# ==============================================================================
# CALLBACK HANDLER
# ==============================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    await clean_expired_basket(user.id)
    u_data = await get_user_data(user.id)
    catalog = await get_catalog_from_db()

    for key in ['awaiting_new_price', 'awaiting_photo', 'add_prod_step', 'awaiting_broadcast', 'awaiting_topup_amount']:
        context.user_data.pop(key, None)

    if data == 'admin_panel' and user.id == ADMIN_ID:
        await query.answer()
        await admin_panel_cmd(update, context)

    elif data == 'admin_users_list' and user.id == ADMIN_ID:
        await query.answer()
        await show_users_management(update, context)

    elif data.startswith('adm_usr_mgmt_') and user.id == ADMIN_ID:
        await query.answer()
        target_uid = int(data.replace('adm_usr_mgmt_', ''))
        await show_single_user_mgmt(update, context, target_uid)

    elif data.startswith('adm_usr_addbal_') and user.id == ADMIN_ID:
        parts = data.split('_')
        target_uid, amount = int(parts[3]), float(parts[4])
        target_data = await get_user_data(target_uid)
        target_data['balance'] += amount
        await save_user_data(target_uid, target_data)
        await query.answer(f"✅ Pridėta {amount}€ vartotojui {target_uid}", show_alert=True)
        await show_single_user_mgmt(update, context, target_uid)

    elif data.startswith('adm_usr_subbal_') and user.id == ADMIN_ID:
        parts = data.split('_')
        target_uid, amount = int(parts[3]), float(parts[4])
        target_data = await get_user_data(target_uid)
        target_data['balance'] = max(0.0, target_data['balance'] - amount)
        await save_user_data(target_uid, target_data)
        await query.answer(f"✅ Atimta {amount}€ iš vartotojo {target_uid}", show_alert=True)
        await show_single_user_mgmt(update, context, target_uid)

    elif data.startswith('adm_usr_zerobal_') and user.id == ADMIN_ID:
        target_uid = int(data.replace('adm_usr_zerobal_', ''))
        target_data = await get_user_data(target_uid)
        target_data['balance'] = 0.0
        await save_user_data(target_uid, target_data)
        await query.answer(f"✅ Vartotojo {target_uid} balansas anuliuotas", show_alert=True)
        await show_single_user_mgmt(update, context, target_uid)

    elif data.startswith('adm_usr_delete_') and user.id == ADMIN_ID:
        target_uid = int(data.replace('adm_usr_delete_', ''))
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM users WHERE user_id = ?", (target_uid,))
            await conn.commit()
        await query.answer(f"🗑️ Vartotojas {target_uid} ištrintas iš DB", show_alert=True)
        await show_users_management(update, context)

    elif data == 'admin_inv' and user.id == ADMIN_ID:
        await query.answer()
        await inventory_cmd(update, context)

    elif data == 'admin_sol_wallets' and user.id == ADMIN_ID:
        await query.answer()
        active_orders = await get_all_active_orders()
        if not active_orders:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')]] )
            await query.edit_message_text("💰 **Aktyvūs SOL adresai:**\n\nŠiuo metu nėra aktyvių užsakymų.", reply_markup=kb, parse_mode='Markdown')
            return

        msg = "💰 **Aktyvūs SOL adresai ir balansai:**\n\n"
        total_sol_balance = 0.0

        for order_idx, order in active_orders.items():
            address = get_order_address(order_idx, order)
            bal = await check_address_balance_raw(address)
            total_sol_balance += bal
            msg += f"• Tipas: **{order.get('type')}** | Vartotojas: `{order.get('user_id')}`\n  Adresas: `{address}`\n  Balansas: **{bal:.4f} SOL**\n\n"

        msg += f"📊 **Iš viso lėšų:** {total_sol_balance:.4f} SOL"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Perkelti visus SOL į Main Wallet", callback_data='admin_sweep_now')],
            [InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')]
        ])
        await query.edit_message_text(msg, reply_markup=kb, parse_mode='Markdown')

    elif data == 'admin_sweep_now' and user.id == ADMIN_ID:
        await query.answer("Vykdomas lėšų perkėlimas...")
        successful_sweeps = 0
        active_orders = await get_all_active_orders()
        for order_idx, order in active_orders.items():
            try:
                wallet_index = order.get("wallet_index")
                if wallet_index is not None:
                    res = await send_sol_to_main_wallet(int(wallet_index))
                    if res:
                        successful_sweeps += 1
                    await asyncio.sleep(0.3)
            except Exception as e:
                logging.error(f"Klaida sweep: {e}")
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Atgal į Admin Skydelį", callback_data='admin_panel')]])
        await query.edit_message_text(f"✅ Sėkmingai pervesta lėšų iš **{successful_sweeps}** adresų.", reply_markup=kb, parse_mode='Markdown')

    elif data == 'admin_buyers_history' and user.id == ADMIN_ID:
        await query.answer()
        await show_buyers_history(update, context)

    elif data.startswith('adm_usr_hist_') and user.id == ADMIN_ID:
        await query.answer()
        target_uid = int(data.replace('adm_usr_hist_', ''))
        await show_user_detail_history(update, context, target_uid)

    elif data.startswith('adm_get_usr_ph_') and user.id == ADMIN_ID:
        await query.answer()
        parts = data.split('_')
        target_uid, hist_idx = int(parts[4]), int(parts[5])
        target_data = await get_user_data(target_uid)
        history = target_data.get('history', [])
        if 0 <= hist_idx < len(history):
            item = history[hist_idx]
            if item.get('photo'):
                try:
                    await context.bot.send_photo(chat_id=ADMIN_ID, photo=item['photo'], caption=f"🔍 Vartotojo `{target_uid}` pirkinys: **{item.get('name')}**", parse_mode='Markdown')
                except Exception:
                    pass

    elif data == 'admin_broadcast_start' and user.id == ADMIN_ID:
        await query.answer()
        context.user_data['awaiting_broadcast'] = True
        await query.edit_message_text("📢 Atsiųskite žinutę arba nuotrauką visiems vartotojams:", parse_mode='Markdown')

    elif data == 'admin_restart_users_confirm' and user.id == ADMIN_ID:
        await query.answer()
        await restart_users_data(update, context)

    elif data == 'admin_edit_price_start' and user.id == ADMIN_ID:
        await query.answer()
        keyboard = [[InlineKeyboardButton(f"🏙 {city['name']}", callback_data=f"adm_ep_c_{city_id}")] for city_id, city in catalog.items()]
        keyboard.append([InlineKeyboardButton("🔙 Atgal", callback_data='admin_panel')])
        await query.edit_message_text("🏷 Pasirinkite miestą:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_ep_c_') and user.id == ADMIN_ID:
        await query.answer()
        city_id = data.replace('adm_ep_c_', '')
        city = catalog.get(city_id, {})
        keyboard = [[InlineKeyboardButton(f"{prod['name']} ({prod['price']:.2f}€)", callback_data=f"adm_ep_p_{city_id}_{prod['id']}")] for prod in city.get("products", [])]
        keyboard.append([InlineKeyboardButton("🔙 Atgal", callback_data='admin_edit_price_start')])
        await query.edit_message_text("🏷 Pasirinkite prekę:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_ep_p_') and user.id == ADMIN_ID:
        await query.answer()
        parts = data.split('_')
        context.user_data['edit_city_id'] = parts[3]
        context.user_data['edit_prod_id'] = "_".join(parts[4:])
        context.user_data['awaiting_new_price'] = True
        await query.edit_message_text(f"✏ Įrašykite **naują kainą** eurais prekei `{context.user_data['edit_prod_id']}`:", parse_mode='Markdown')

    elif data == 'admin_add_prod_start' and user.id == ADMIN_ID:
        await query.answer()
        keyboard = [
            [InlineKeyboardButton("🏙️ Kėdainiai", callback_data="adm_np_c_kedainiai")],
            [InlineKeyboardButton("🏙️ Kaunas", callback_data="adm_np_c_kaunas")],
            [InlineKeyboardButton("🏙️ Vilnius", callback_data="adm_np_c_vilnius")],
            [InlineKeyboardButton("🏙 Klaipėda", callback_data="adm_np_c_klaipeda")],
            [InlineKeyboardButton("🔙 Atgal", callback_data='admin_panel')]
        ]
        await query.edit_message_text("➕ Pasirinkite miestą:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_np_c_') and user.id == ADMIN_ID:
        await query.answer()
        context.user_data['new_city_id'] = data.replace('adm_np_c_', '')
        context.user_data['add_prod_step'] = 'id'
        await query.edit_message_text("✏️ Įveskite prekės **ID** (be tarpų):", parse_mode='Markdown')

    elif data == 'admin_add_photo_start' and user.id == ADMIN_ID:
        await query.answer()
        keyboard = [[InlineKeyboardButton(f"🏙 {city['name']}", callback_data=f"adm_addph_c_{city_id}")] for city_id, city in catalog.items()]
        keyboard.append([InlineKeyboardButton("🔙 Atgal", callback_data='admin_panel')])
        await query.edit_message_text("📸 Pasirinkite miestą:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_addph_c_') and user.id == ADMIN_ID:
        await query.answer()
        city_id = data.replace('adm_addph_c_', '')
        city = catalog.get(city_id, {})
        keyboard = [[InlineKeyboardButton(f"{prod['name']} (Likutis: {len(prod.get('photos', []))} vnt.)", callback_data=f"adm_addph_p_{city_id}_{prod['id']}")] for prod in city.get("products", [])]
        keyboard.append([InlineKeyboardButton("🔙 Atgal", callback_data='admin_add_photo_start')])
        await query.edit_message_text("📸 Pasirinkite prekę:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_addph_p_') and user.id == ADMIN_ID:
        await query.answer()
        parts = data.split('_')
        context.user_data['photo_city_id'] = parts[3]
        context.user_data['photo_prod_id'] = "_".join(parts[4:])
        context.user_data['awaiting_photo'] = True
        await query.edit_message_text(f"📸 Atsiųskite nuotrauką prekei `{context.user_data['photo_prod_id']}`:", parse_mode='Markdown')

    elif data == 'admin_del_prod_start' and user.id == ADMIN_ID:
        await query.answer()
        keyboard = [[InlineKeyboardButton(f"🏙 {city['name']}", callback_data=f"adm_del_c_{city_id}")] for city_id, city in catalog.items()]
        keyboard.append([InlineKeyboardButton("🔙 Atgal", callback_data='admin_panel')])
        await query.edit_message_text("🗑️ Pasirinkite miestą:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_del_c_') and user.id == ADMIN_ID:
        await query.answer()
        city_id = data.replace('adm_del_c_', '')
        city = catalog.get(city_id, {})
        keyboard = []
        for prod in city.get("products", []):
            keyboard.append([
                InlineKeyboardButton(f"❌ Trinti: {prod['name']}", callback_data=f"adm_del_act_all_{city_id}_{prod['id']}"),
                InlineKeyboardButton(f"🖼 Nuotraukas", callback_data=f"adm_del_act_ph_{city_id}_{prod['id']}")
            ])
        keyboard.append([InlineKeyboardButton("🔙 Atgal", callback_data='admin_del_prod_start')])
        await query.edit_message_text("🗑 Pasirinkite veiksmą:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('adm_del_act_') and user.id == ADMIN_ID:
        await query.answer()
        parts = data.split('_')
        mode, city_id, prod_id = parts[3], parts[4], "_".join(parts[5:])
        async with aiosqlite.connect(DB_PATH) as conn:
            if mode == "ph":
                cursor = await conn.execute("DELETE FROM product_photos WHERE city_id = ? AND product_id = ?", (city_id, prod_id))
                msg = f"🗑 Pašalintos nuotraukos."
            else:
                await conn.execute("DELETE FROM product_photos WHERE city_id = ? AND product_id = ?", (city_id, prod_id))
                await conn.execute("DELETE FROM products WHERE city_id = ? AND product_id = ?", (city_id, prod_id))
                msg = "❌ Prekė ištrinta!"
            await conn.commit()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙ Grįžti", callback_data='admin_panel')]])
        await query.edit_message_text(msg, reply_markup=kb, parse_mode='Markdown')

    elif data == 'home':
        await query.answer()
        await send_main_home(update)

    elif data == 'sold_out':
        await query.answer("❌ Ši prekė šiuo metu išparduota arba rezervuota.", show_alert=True)

    elif data == 'menu_shop':
        await query.answer()
        await query.edit_message_text("🏙 **Pasirinkite miestą**", reply_markup=cities_keyboard(catalog), parse_mode='Markdown')

    elif data == 'menu_profile':
        await query.answer()
        basket = u_data.get('basket', [])
        history = u_data.get('history', [])
        total_basket_price = sum(item.get('price', 0) for item in basket)

        text = (
            f"👤 **Jūsų profilis**\n\n"
            f"🆔 ID: `{user.id}`\n"
            f"💵 Balansas: **{u_data['balance']:.2f} EUR**\n"
            f"🛒 Išleista: **{u_data['spent']:.2f} EUR**\n"
            f"⭐ Statusas: **{u_data['status']}**\n\n"
            f"🧺 **Krepšelis ({len(basket)} prekės, viso: {total_basket_price:.2f}€):**\n"
        )
        if not basket:
            text += "_Krepšelis tuščias_\n"
        else:
            for item in basket:
                text += f"• {item.get('name')} — {item.get('price', 0):.2f}€\n"

        text += f"\n📜 **Pirkimų istorija ({len(history)}):**\n"
        history_buttons = []
        if not history:
            text += "_Istorija tuščia_\n"
        else:
            for idx, h in enumerate(reversed(history[-5:])):
                real_idx = len(history) - 1 - idx
                text += f"• {h.get('date', '')} | {h.get('name', '')} ({h.get('price', 0):.2f}€)\n"
                if h.get('photo'):
                    history_buttons.append([InlineKeyboardButton(f"🖼️ Peržiūrėti photo #{len(history)-idx}", callback_data=f"show_hist_photo_{real_idx}")])

        main_kb = profile_basket_keyboard(len(basket))
        all_buttons = history_buttons + list(main_kb.inline_keyboard)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(all_buttons), parse_mode='Markdown')

    elif data.startswith('show_hist_photo_'):
        await query.answer()
        hist_idx = int(data.replace('show_hist_photo_', ''))
        history = u_data.get('history', [])
        if 0 <= hist_idx < len(history):
            item = history[hist_idx]
            if item.get('photo'):
                try:
                    await context.bot.send_photo(chat_id=user.id, photo=item['photo'], caption=f"📦 Pirkinys: {item.get('name')}")
                except Exception:
                    pass

    elif data == 'clear_basket':
        u_data['basket'] = []
        await save_user_data(user.id, u_data)
        await query.answer("🗑️ Krepšelis išvalytas!", show_alert=True)
        await send_main_home(update)

    elif data == 'menu_topup':
        await query.answer()
        context.user_data['awaiting_topup_amount'] = True
        text = (
            "💳 **Balanso papildymas**\n\n"
            "Įveskite norimą papildymo sumą eurais (minimali suma: **5 EUR**):"
        )
        await query.edit_message_text(text, reply_markup=invoice_keyboard(), parse_mode='Markdown')

    elif data == 'menu_pricelist':
        await query.answer()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Pagrindinis", callback_data='home')]])
        await query.edit_message_text("🏷 **Kainoraštis**\n\nVisos kainos nurodytos parduotuvėje.", reply_markup=kb, parse_mode='Markdown')

    elif data.startswith('city_'):
        await query.answer()
        city_id = data.replace('city_', '')
        city = catalog.get(city_id, {})
        reply_kb = await city_products_keyboard(city_id, catalog)
        await query.edit_message_text(f"🏙 **{city.get('name', 'Miestas')}**\n\nPasirinkite prekę:", reply_markup=reply_kb, parse_mode='Markdown')

    elif data.startswith('prod_'):
        await query.answer()
        parts = data.split('_')
        city_id, prod_id = parts[1], "_".join(parts[2:])
        city = catalog.get(city_id, {})
        prod = next((p for p in city.get("products", []) if p["id"] == prod_id), None)
        if prod:
            cat_str = f"\n🏷️ Kategorija: **{prod.get('category')}**" if prod.get('category') else ""
            text = f"🏙 **{city['name']}**\n**{prod['name']}**{cat_str}\n💰 **{prod['price']:.2f}€**"
            await query.edit_message_text(text, reply_markup=product_detail_keyboard(city_id, prod_id), parse_mode='Markdown')

    elif data.startswith('addbasket_'):
        parts = data.split('_')
        city_id, prod_id = parts[1], "_".join(parts[2:])
        city = catalog.get(city_id, {})
        prod = next((p for p in city.get("products", []) if p["id"] == prod_id), None)
        if prod:
            reserved_total = await get_reserved_count_in_baskets(city_id, prod_id)
            if (len(prod['photos']) - reserved_total) <= 0:
                await query.answer("❌ Liko nepakankamai laisvų vnt.!", show_alert=True)
                return

            u_data['basket'].append({
                "city_id": city_id,
                "opt_id": prod_id,
                "name": prod["name"],
                "price": prod["price"],
                "added_at": time.time()
            })
            await save_user_data(user.id, u_data)
            await query.answer("🛒 Pridėta į krepšelį (45 min)!", show_alert=True)

    elif data.startswith('paynow_'):
        parts = data.split('_')
        city_id, prod_id = parts[1], "_".join(parts[2:])
        city = catalog.get(city_id, {})
        prod = next((p for p in city.get("products", []) if p["id"] == prod_id), None)
        if prod:
            # Patikriname ar yra laisvų vnt.
            reserved_total = await get_reserved_count_in_baskets(city_id, prod_id)
            if (len(prod['photos']) - reserved_total) <= 0:
                await query.answer("❌ Atsiprašome, prekė jau išparduota arba rezervuota kito vartotojo!", show_alert=True)
                return

            sol_price_eur = await get_sol_price_in_eur()
            if sol_price_eur <= 0:
                await query.answer("❌ Nepavyko gauti SOL kainos.", show_alert=True)
                return

            required_sol = round(prod["price"] / sol_price_eur, 6)
            wallet_index = await get_next_wallet_counter()
            unique_address = derive_solana_address(wallet_index)

            order_dict = {
                "type": "single",
                "order_index": wallet_index,
                "wallet_index": wallet_index,
                "user_id": user.id,
                "city_id": city_id,
                "opt_id": prod_id,
                "item_name": prod["name"],
                "price_eur": prod["price"],
                "price_sol": required_sol,
                "address": unique_address,
                "created_at": time.time()
            }
            await save_active_order(wallet_index, user.id, order_dict)
            await query.answer()

            text = (
                "🧾 **Mokėjimo sąskaita (Rezervuota 45 min)**\n\n"
                f"• Prekė: **{prod['name']}** ({prod['price']:.2f}€)\n\n"
                f"**Perveskite tiksliai: {required_sol:.6f} SOL**\n\n"
                f"**Adresas:**\n`{unique_address}`"
            )
            await query.edit_message_text(text, reply_markup=invoice_keyboard(), parse_mode='Markdown')

    elif data == 'pay_basket_sol':
        basket = u_data.get('basket', [])
        if not basket:
            await query.answer("❌ Krepšelis tuščias!", show_alert=True)
            return

        # Patikriname visų krepšelio prekių likučius
        for item in basket:
            c_id = item["city_id"]
            p_id = item["opt_id"]
            city = catalog.get(c_id, {})
            prod = next((p for p in city.get("products", []) if p["id"] == p_id), None)
            if prod:
                reserved_total = await get_reserved_count_in_baskets(c_id, p_id)
                if (len(prod['photos']) - reserved_total) < 0:
                    await query.answer(f"❌ Prekė '{item['name']}' ką tik išparduota!", show_alert=True)
                    return

        total_price_eur = sum(item.get('price', 0) for item in basket)
        sol_price_eur = await get_sol_price_in_eur()
        if sol_price_eur <= 0:
            await query.answer("❌ Nepavyko gauti SOL kainos.", show_alert=True)
            return

        required_sol = round(total_price_eur / sol_price_eur, 6)
        wallet_index = await get_next_wallet_counter()
        unique_address = derive_solana_address(wallet_index)

        basket_items_order = [{"city_id": i["city_id"], "opt_id": i["opt_id"], "item_name": i["name"], "price_eur": i["price"]} for i in basket]
        order_dict = {
            "type": "basket",
            "order_index": wallet_index,
            "wallet_index": wallet_index,
            "user_id": user.id,
            "items": basket_items_order,
            "price_eur": total_price_eur,
            "price_sol": required_sol,
            "address": unique_address,
            "created_at": time.time()
        }
        await save_active_order(wallet_index, user.id, order_dict)
        await query.answer()

        text = (
            "🧾 **Krepšelio sąskaita (Rezervuota 45 min)**\n\n"
            f"• Suma: **{total_price_eur:.2f}€**\n"
            f"**Perveskite tiksliai: {required_sol:.6f} SOL**\n\n"
            f"`{unique_address}`"
        )
        await query.edit_message_text(text, reply_markup=invoice_keyboard(), parse_mode='Markdown')

    elif data == 'pay_basket_balance':
        basket = u_data.get('basket', [])
        if not basket:
            await query.answer("❌ Krepšelis tuščias!", show_alert=True)
            return

        total_price_eur = sum(item.get('price', 0) for item in basket)
        if u_data['balance'] < total_price_eur:
            await query.answer("❌ Nepakankamas balansas!", show_alert=True)
            return

        u_data['balance'] -= total_price_eur
        u_data['spent'] += total_price_eur
        delivered_items = []

        for item in basket:
            unique_photo, remaining = await pop_product_photo(item["city_id"], item["opt_id"])
            if unique_photo:
                delivered_items.append((item["name"], unique_photo))
                u_data['history'].append({"name": item['name'], "price": item['price'], "photo": unique_photo, "date": time.strftime("%Y-%m-%d %H:%M")})
                if remaining == 0:
                    await notify_stock_empty(context, item['name'], item['city_id'])

        u_data['basket'] = []
        await save_user_data(user.id, u_data)
        await query.answer("🎉 Apmokėta iš balanso!", show_alert=True)
        await query.edit_message_text("🎉 **Mokėjimas iš balanso atliktas!**\n\nSiunčiamos nuotraukos...", parse_mode='Markdown')

        for item_name, photo_id in delivered_items:
            try:
                await context.bot.send_photo(chat_id=user.id, photo=photo_id, caption=f"📦 Pirkinys: {item_name}")
            except Exception:
                pass

    elif data == 'cancel_confirm':
        await query.answer()
        await delete_user_active_orders(user.id)
        await send_main_home(update)


# ==============================================================================
# BOT RUNNER & THREAD
# ==============================================================================

async def main_bot_runner():
    await init_db()
    bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel_cmd))
    bot_app.add_handler(CommandHandler("inventory", inventory_cmd))
    bot_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    bot_app.add_handler(CommandHandler("sweep", sweep_all_wallets))
    bot_app.add_handler(CommandHandler("restart_users", restart_users_data))
    bot_app.add_handler(CommandHandler("set_product", set_product_cmd))
    bot_app.add_handler(CommandHandler("delete_product", delete_product_cmd))
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, handle_admin_text_and_photos))

    if bot_app.job_queue:
        bot_app.job_queue.run_repeating(auto_check_payments, interval=10, first=3)

    async with bot_app:
        await bot_app.start()
        await bot_app.updater.start_polling()
        while True:
            await asyncio.sleep(3600)


def start_telegram_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_bot_runner())


bot_thread = Thread(target=start_telegram_bot_thread, daemon=True)
bot_thread.start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)