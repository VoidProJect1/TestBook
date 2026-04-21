#!/bin/bash
# =====================================================
#  SearXNG Self-Host Installer
#  Fixed for Oracle Linux with Python 3.9
#  Uses Docker method — most reliable approach
# =====================================================

set -e

echo ""
echo "====================================="
echo "  SearXNG Installer (Docker method)"
echo "====================================="
echo ""

# ── Check Python version ───────────────────────────────
PYVER=$(python3 --version 2>&1 | awk '{print $2}')
echo "[*] Python version: $PYVER"

# ── Install Docker if not present ─────────────────────
if ! command -v docker &>/dev/null; then
    echo "[*] Installing Docker..."
    sudo dnf install -y dnf-utils
    sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    sudo dnf install -y docker-ce docker-ce-cli containerd.io
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo usermod -aG docker $USER
    echo "[*] Docker installed"
else
    echo "[*] Docker already installed: $(docker --version)"
fi

# ── Make sure Docker is running ────────────────────────
sudo systemctl start docker

# ── Create SearXNG config directory ───────────────────
SEARXNG_DIR="$HOME/searxng-docker"
mkdir -p "$SEARXNG_DIR"

# ── settings.yml ──────────────────────────────────────
cat > "$SEARXNG_DIR/settings.yml" << 'YAML'
use_default_settings: true

general:
  debug: false
  instance_name: "VoidAI Search"

search:
  safe_search: 0
  autocomplete: ""
  default_lang: "en"

server:
  port: 8080
  bind_address: "0.0.0.0"
  secret_key: "voidai_searxng_secret_please_change"
  limiter: false
  public_instance: false
  image_proxy: false

outgoing:
  request_timeout: 8.0
  max_request_timeout: 15.0
  pool_connections: 10
  pool_maxsize: 20

engines:
  - name: google
    engine: google
    shortcut: g
    timeout: 8.0

  - name: bing
    engine: bing
    shortcut: b
    timeout: 8.0

  - name: duckduckgo
    engine: duckduckgo
    shortcut: d
    timeout: 8.0

  - name: brave
    engine: brave
    shortcut: br
    timeout: 8.0

  - name: wikipedia
    engine: wikipedia
    shortcut: w
    timeout: 5.0
YAML

# ── limiter.toml (disable rate limiting) ──────────────
cat > "$SEARXNG_DIR/limiter.toml" << 'TOML'
[real_ip]
  x_for = 1
  ipv4_prefix = 32
  ipv6_prefix = 48

[botdetection.ip_limit]
  link_token = false
TOML

echo "[*] Config files created at $SEARXNG_DIR"

# ── Stop existing container if running ────────────────
sudo docker stop searxng 2>/dev/null || true
sudo docker rm   searxng 2>/dev/null || true

# ── Pull and run SearXNG container ────────────────────
echo "[*] Pulling SearXNG Docker image..."
sudo docker pull searxng/searxng:latest

echo "[*] Starting SearXNG container..."
sudo docker run -d \
    --name searxng \
    --restart always \
    -p 8080:8080 \
    -v "$SEARXNG_DIR/settings.yml:/etc/searxng/settings.yml:ro" \
    -v "$SEARXNG_DIR/limiter.toml:/etc/searxng/limiter.toml:ro" \
    -e SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
    searxng/searxng:latest

echo "[*] Waiting for SearXNG to start..."
sleep 8

# ── Test it ───────────────────────────────────────────
echo "[*] Testing SearXNG..."
RESULT=$(curl -s "http://127.0.0.1:8080/search?q=test&format=json" | head -c 100)
if echo "$RESULT" | grep -q "results\|query"; then
    echo "[OK] SearXNG is working!"
else
    echo "[!] SearXNG might still be starting. Test manually:"
    echo "    curl 'http://127.0.0.1:8080/search?q=test&format=json'"
fi

# ── Open firewall port ────────────────────────────────
echo ""
echo "[*] Opening firewall port 8080..."
sudo firewall-cmd --permanent --add-port=8080/tcp 2>/dev/null && \
sudo firewall-cmd --reload 2>/dev/null && \
echo "[OK] Port 8080 opened" || \
echo "[!] Firewall command failed — open port 8080 manually in OCI console"

echo ""
echo "====================================="
echo "  SearXNG Install Complete!"
echo "====================================="
echo ""
echo "Local URL:   http://127.0.0.1:8080"
echo "Public URL:  http://$(curl -s ifconfig.me 2>/dev/null || echo YOUR_VM_IP):8080"
echo ""
echo "Test API:    curl 'http://127.0.0.1:8080/search?q=hello&format=json'"
echo "Browser UI:  http://127.0.0.1:8080/search?q=hello"
echo ""
echo "Docker commands:"
echo "  sudo docker ps                  # check running"
echo "  sudo docker logs searxng        # view logs"
echo "  sudo docker restart searxng     # restart"
echo "  sudo docker stop searxng        # stop"
echo ""
echo "NOTE: If using Oracle Cloud, also open port 8080 in:"
echo "  OCI Console → Networking → VCN → Security Lists → Add Ingress Rule"
echo ""
