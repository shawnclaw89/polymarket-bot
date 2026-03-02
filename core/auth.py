"""
Kalshi Authentication — RSA API Key setup.

Kalshi uses RSA key-pair auth:
  1. You generate a key pair (we can do this for you)
  2. Upload the PUBLIC key to kalshi.com → Settings → API Keys
  3. Store the PRIVATE key locally (never share it, never commit it)
  4. Pass the key ID + private key to this module

How to get your API Key ID:
  - Log in to kalshi.com
  - Go to Settings → API Keys
  - Click "Create API Key"
  - Upload the public key we generated (kalshi_public_key.pem)
  - Copy the Key ID shown
  - Paste it into config.yaml as api_key_id
"""
import logging
import os

log = logging.getLogger(__name__)


def init(api_key_id: str, private_key_path: str, host: str) -> bool:
    """
    Initialize the authenticated Kalshi client.
    Returns True if successful.
    """
    import core.api as api_module

    if not api_key_id or not private_key_path:
        log.info("No API credentials configured — running in read-only mode.")
        return False

    if not os.path.exists(private_key_path):
        log.warning(f"Private key file not found: {private_key_path}")
        log.warning("Running in read-only mode. See README to set up API keys.")
        return False

    try:
        import kalshi_python

        config = kalshi_python.Configuration(host=host)
        client = kalshi_python.KalshiClient(config)
        client.set_kalshi_auth(api_key_id, private_key_path)

        # Wire up sub-APIs
        portfolio = kalshi_python.PortfolioApi(client)
        markets   = kalshi_python.MarketsApi(client)

        # Test the connection
        balance = portfolio.get_balance()
        log.info(f"Authenticated ✅ | Balance: ${balance.balance / 100:.2f}")

        # Store in api module
        api_module._client        = client
        api_module._portfolio_api = portfolio
        api_module._markets_api   = markets
        return True

    except Exception as e:
        log.error(f"Auth failed: {e}")
        log.warning("Running in read-only/paper mode.")
        return False


def generate_keys(output_dir: str = "."):
    """
    Generate a new RSA key pair for Kalshi API auth.
    Saves:
      kalshi_private_key.pem  ← keep this secret, add to config.yaml
      kalshi_public_key.pem   ← upload this to kalshi.com Settings → API Keys
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    priv_path = os.path.join(output_dir, "kalshi_private_key.pem")
    pub_path  = os.path.join(output_dir, "kalshi_public_key.pem")

    with open(priv_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    os.chmod(priv_path, 0o600)

    with open(pub_path, "wb") as f:
        f.write(private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))

    print(f"\n✅ Keys generated!")
    print(f"   Private key: {priv_path}  ← keep secret")
    print(f"   Public key:  {pub_path}   ← upload to Kalshi\n")
    print("Next steps:")
    print("  1. Go to https://kalshi.com → Settings → API Keys")
    print("  2. Click 'Create API Key'")
    print(f"  3. Upload {pub_path}")
    print("  4. Copy the Key ID and paste it into config.yaml as api_key_id")
    print("  5. Run: python3 bot.py --once\n")

    return priv_path, pub_path
