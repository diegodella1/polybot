"""On-chain wallet operations: balances, redeem CTF tokens, ERC20 transfers."""

import asyncio
import logging
import os
from dataclasses import dataclass, field

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from db import get_db

logger = logging.getLogger(__name__)

# --- RPC config ---
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
]

# --- Contract addresses (Polygon mainnet) ---
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")  # USDC.e (bridged)
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")  # ConditionalTokens
PROXY_WALLET = Web3.to_checksum_address("0x73abf22e40DA48E684f5CC705F5d759A64e0b1E6")  # Polymarket proxy

# Minimal ABIs
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CTF_ABI = [
    {
        "inputs": [{"name": "id", "type": "uint256"}, {"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "name": "getCollectionId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass
class WalletResult:
    success: bool
    tx_hash: str | None = None
    error: str = ""
    details: dict = field(default_factory=dict)


# --- Web3 connection ---

def _get_w3() -> Web3:
    """Get Web3 instance with fallback RPCs."""
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("All Polygon RPCs failed")


def _get_account():
    """Get account from private key."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    w3 = _get_w3()
    account = w3.eth.account.from_key(pk)
    return w3, account


# --- Read functions (no gas) ---

def get_eoa_usdc_balance() -> float:
    """Get USDC.e balance of EOA wallet."""
    w3, account = _get_account()
    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    raw = usdc.functions.balanceOf(account.address).call()
    return raw / 1e6


def get_exchange_usdc_balance() -> float:
    """Get USDC.e balance in Polymarket proxy wallet (available to trade)."""
    w3 = _get_w3()
    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    raw = usdc.functions.balanceOf(PROXY_WALLET).call()
    return raw / 1e6


def get_eoa_matic_balance() -> float:
    """Get MATIC (POL) balance of EOA for gas."""
    w3, account = _get_account()
    raw = w3.eth.get_balance(account.address)
    return float(w3.from_wei(raw, "ether"))


def get_token_balance(token_id: str) -> float:
    """Get CTF ERC1155 token balance."""
    w3, account = _get_account()
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    token_int = int(token_id) if not token_id.startswith("0x") else int(token_id, 16)
    raw = ctf.functions.balanceOf(token_int, account.address).call()
    return raw / 1e6


def is_condition_resolved(condition_id: str) -> bool:
    """Check if condition is resolved (payoutDenominator > 0)."""
    w3 = _get_w3()
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    denom = ctf.functions.payoutDenominator(cid_bytes).call()
    return denom > 0


def get_position_id(condition_id: str, index_set: int) -> int:
    """Compute position ID by calling getCollectionId on-chain + keccak256.

    CTF uses ECMH (alt_bn128) for collection IDs — can't replicate off-chain.
    """
    w3 = _get_w3()
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    parent = b"\x00" * 32  # parentCollectionId = 0 for root

    collection_id = ctf.functions.getCollectionId(parent, cid_bytes, index_set).call()

    # positionId = keccak256(abi.encodePacked(collateralToken, collectionId))
    encoded = Web3.solidity_keccak(
        ["address", "bytes32"],
        [USDC_E, collection_id],
    )
    return int.from_bytes(encoded, "big")


def scan_redeemable_tokens() -> list[dict]:
    """Scan DB for winning trades with on-chain token balances > 0.

    Returns list of {condition_id, side, token_id, balance, trade_ids}.
    """
    import sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "polybot.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get winning trades grouped by condition_id + side
    rows = conn.execute(
        """SELECT condition_id, side, token_id, GROUP_CONCAT(id) as trade_ids
           FROM trades
           WHERE outcome = 'win'
           GROUP BY condition_id, side"""
    ).fetchall()
    conn.close()

    redeemable = []
    w3, account = _get_account()
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)

    for row in rows:
        condition_id = row["condition_id"]
        side = row["side"]

        try:
            # Compute position ID on-chain
            index_set = 1 if side == "up" else 2
            pos_id = get_position_id(condition_id, index_set)

            # Check balance
            balance_raw = ctf.functions.balanceOf(pos_id, account.address).call()
            if balance_raw > 0:
                balance = balance_raw / 1e6
                redeemable.append({
                    "condition_id": condition_id,
                    "side": side,
                    "token_id": row["token_id"],
                    "index_set": index_set,
                    "balance": balance,
                    "trade_ids": row["trade_ids"],
                })
        except Exception as e:
            logger.warning("Error scanning condition %s: %s", condition_id[:16], e)

    return redeemable


# --- Write functions (use gas, run in thread) ---

async def redeem_positions(condition_id: str, index_sets: list[int]) -> WalletResult:
    """Redeem resolved CTF positions for USDC.e.

    Args:
        condition_id: The condition ID (hex string).
        index_sets: List of index sets to redeem (1=Yes/Up, 2=No/Down).
    """
    try:
        result = await asyncio.to_thread(_redeem_sync, condition_id, index_sets)
        return result
    except Exception as e:
        logger.error("Redeem failed for %s: %s", condition_id[:16], e)
        return WalletResult(success=False, error=str(e))


def _redeem_sync(condition_id: str, index_sets: list[int]) -> WalletResult:
    """Synchronous redeem — called via asyncio.to_thread."""
    w3, account = _get_account()
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    parent = b"\x00" * 32

    # Check if resolved first
    denom = ctf.functions.payoutDenominator(cid_bytes).call()
    if denom == 0:
        return WalletResult(success=False, error="Condition not yet resolved")

    # Build transaction
    tx = ctf.functions.redeemPositions(
        USDC_E,
        parent,
        cid_bytes,
        index_sets,
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 300_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "chainId": 137,
    })

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    hex_hash = tx_hash.hex()
    if receipt["status"] == 1:
        logger.info("Redeem OK: %s (condition=%s)", hex_hash, condition_id[:16])
        return WalletResult(success=True, tx_hash=hex_hash)
    else:
        logger.error("Redeem reverted: %s", hex_hash)
        return WalletResult(success=False, tx_hash=hex_hash, error="Transaction reverted")


async def transfer_usdc(to_address: str, amount_usd: float) -> WalletResult:
    """Transfer USDC.e to an external address."""
    if not Web3.is_address(to_address):
        return WalletResult(success=False, error="Invalid address")
    if amount_usd <= 0:
        return WalletResult(success=False, error="Amount must be positive")

    try:
        result = await asyncio.to_thread(_transfer_sync, to_address, amount_usd)
        return result
    except Exception as e:
        logger.error("Transfer failed: %s", e)
        return WalletResult(success=False, error=str(e))


def _transfer_sync(to_address: str, amount_usd: float) -> WalletResult:
    """Synchronous ERC20 transfer — called via asyncio.to_thread."""
    w3, account = _get_account()
    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    # Check balance first
    balance_raw = usdc.functions.balanceOf(account.address).call()
    amount_raw = int(amount_usd * 1e6)

    if balance_raw < amount_raw:
        return WalletResult(
            success=False,
            error=f"Insufficient balance: ${balance_raw / 1e6:.2f} < ${amount_usd:.2f}",
        )

    to = Web3.to_checksum_address(to_address)
    tx = usdc.functions.transfer(to, amount_raw).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 100_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "chainId": 137,
    })

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    hex_hash = tx_hash.hex()
    if receipt["status"] == 1:
        logger.info("Transfer OK: $%.2f → %s (tx=%s)", amount_usd, to_address[:10], hex_hash)
        return WalletResult(success=True, tx_hash=hex_hash, details={"amount": amount_usd, "to": to_address})
    else:
        logger.error("Transfer reverted: %s", hex_hash)
        return WalletResult(success=False, tx_hash=hex_hash, error="Transaction reverted")
