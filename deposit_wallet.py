"""
Helper to check deposit wallet status and compute address.
Run this after deploying the deposit wallet via Polymarket website.
"""
from config import PRIVATE_KEY, compute_deposit_wallet, DEPOSIT_WALLET_FACTORY
from eth_account import Account
from web3 import Web3
import json


def check():
    if not PRIVATE_KEY:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
        return

    eoa = Account.from_key(PRIVATE_KEY).address
    dw = compute_deposit_wallet(eoa)

    from config import RPC_URLS
    w3 = None
    for rpc_url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if w3.is_connected():
                print(f"  Connected to RPC: {rpc_url}")
                break
        except Exception:
            continue
    if w3 is None or not w3.is_connected():
        print("ERROR: Could not connect to any Polygon RPC endpoint")
        return
    usdc = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
    usdc_abi = json.loads('[{"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
    usdc_ctr = w3.eth.contract(address=usdc, abi=usdc_abi)

    code = w3.eth.get_code(dw)
    deployed = len(code) > 2

    print("=" * 55)
    print("  Polymarket Deposit Wallet Status")
    print("=" * 55)
    print(f"  Signer EOA:            {eoa}")
    print(f"  Deposit Wallet:        {dw}")
    print(f"  Deployed:              {deployed}")
    print(f"  Factory:               {DEPOSIT_WALLET_FACTORY}")
    print(f"  EOA POL balance:       {w3.eth.get_balance(eoa) / 1e18:.4f}")
    print(f"  EOA USDC balance:      {usdc_ctr.functions.balanceOf(eoa).call() / 1e6:.2f}")

    if deployed:
        dw_usdc = usdc_ctr.functions.balanceOf(dw).call() / 1e6
        print(f"  Deposit Wallet USDC:   {dw_usdc:.2f}")
    else:
        print(f"  Deposit Wallet USDC:   N/A (not deployed)")

    print()
    if not deployed:
        print("  Next step:")
        print("  1. Withdraw USDC from old Polymarket account to EOA")
        print(f"     EOA: {eoa}")
        print("  2. Go to https://polymarket.com — connect MetaMask with this EOA")
        print("  3. Click Deposit → the deposit wallet auto-deploys")
        print("  4. Re-run this script to confirm")
    else:
        print("  Deposit wallet is deployed. You can now run the bot.")
        if usdc_ctr.functions.balanceOf(dw).call() / 1e6 < 1:
            print("  WARNING: Low USDC balance. Deposit USDC via Polymarket website.")


if __name__ == "__main__":
    check()
