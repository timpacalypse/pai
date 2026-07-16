#!/usr/bin/env bash
# ─── AEGIS Kiosk Setup ───────────────────────────────────────────────────────
# Run this script ON THE LAPTOP that will display the dashboard.
# It sets up Chromium in kiosk mode, auto-launching on boot.
#
# Usage:
#   chmod +x kiosk-setup.sh
#   sudo ./kiosk-setup.sh
#
# After running, reboot the laptop and it will auto-display the dashboard.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TANK_IP="192.168.0.58"
DASHBOARD_URL="http://${TANK_IP}:3000/aegis.html"
KIOSK_USER="${SUDO_USER:-$(whoami)}"
# ──────────────────────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════════╗"
echo "║  AEGIS Kiosk Setup                          ║"
echo "║  URL: $DASHBOARD_URL"
echo "║  User: $KIOSK_USER"
echo "╚══════════════════════════════════════════════╝"

# ─── Install packages ─────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y --no-install-recommends \
    chromium-browser \
    unclutter \
    xdotool \
    xserver-xorg \
    x11-xserver-utils

# ─── Disable screen blanking / power management ──────────────────────────────
mkdir -p /etc/X11/xorg.conf.d
cat > /etc/X11/xorg.conf.d/10-no-blanking.conf << 'EOF'
Section "ServerFlags"
    Option "BlankTime" "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime" "0"
EOF

# ─── Disable screen lock / screensaver via lightdm (if present) ──────────────
if command -v gsettings &>/dev/null; then
    sudo -u "$KIOSK_USER" dbus-launch gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
    sudo -u "$KIOSK_USER" dbus-launch gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
fi

# ─── Create autostart directory ──────────────────────────────────────────────
AUTOSTART_DIR="/home/${KIOSK_USER}/.config/autostart"
mkdir -p "$AUTOSTART_DIR"

# ─── Kiosk launcher script ───────────────────────────────────────────────────
KIOSK_SCRIPT="/home/${KIOSK_USER}/.local/bin/aegis-kiosk.sh"
mkdir -p "$(dirname "$KIOSK_SCRIPT")"
cat > "$KIOSK_SCRIPT" << SCRIPT
#!/usr/bin/env bash
# AEGIS Kiosk Launcher

# Wait for network
for i in {1..30}; do
    ping -c1 -W1 ${TANK_IP} &>/dev/null && break
    sleep 1
done

# Disable screen blanking
xset s off
xset -dpms
xset s noblank

# Hide cursor after 3 seconds of inactivity
unclutter -idle 3 -root &

# Kill any existing Chromium instances
pkill -f chromium 2>/dev/null || true
sleep 1

# Clear Chromium crash flags to avoid "restore session" prompts
CHROMIUM_DIR="\$HOME/.config/chromium/Default"
if [[ -f "\$CHROMIUM_DIR/Preferences" ]]; then
    sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "\$CHROMIUM_DIR/Preferences" 2>/dev/null || true
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "\$CHROMIUM_DIR/Preferences" 2>/dev/null || true
fi

# Launch Chromium in kiosk mode
exec chromium-browser \\
    --kiosk \\
    --noerrdialogs \\
    --disable-infobars \\
    --no-first-run \\
    --check-for-update-interval=31536000 \\
    --disable-translate \\
    --disable-features=TranslateUI \\
    --disable-session-crashed-bubble \\
    --disable-component-update \\
    --overscroll-history-navigation=0 \\
    --autoplay-policy=no-user-gesture-required \\
    "${DASHBOARD_URL}"
SCRIPT
chmod +x "$KIOSK_SCRIPT"
chown "$KIOSK_USER:$KIOSK_USER" "$KIOSK_SCRIPT"

# ─── Autostart .desktop entry ────────────────────────────────────────────────
cat > "${AUTOSTART_DIR}/aegis-kiosk.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=AEGIS Kiosk
Exec=${KIOSK_SCRIPT}
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
DESKTOP
chown "$KIOSK_USER:$KIOSK_USER" "${AUTOSTART_DIR}/aegis-kiosk.desktop"

# ─── Auto-login (for GDM/LightDM) ────────────────────────────────────────────
# GDM
if [[ -f /etc/gdm3/custom.conf ]]; then
    sed -i "s/^#.*AutomaticLoginEnable.*/AutomaticLoginEnable=true/" /etc/gdm3/custom.conf
    sed -i "s/^#.*AutomaticLogin .*/AutomaticLogin=${KIOSK_USER}/" /etc/gdm3/custom.conf
    # If lines don't exist, add them
    if ! grep -q "AutomaticLoginEnable" /etc/gdm3/custom.conf; then
        sed -i "/\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=${KIOSK_USER}" /etc/gdm3/custom.conf
    fi
fi

# LightDM
if [[ -f /etc/lightdm/lightdm.conf ]]; then
    sed -i "s/^#.*autologin-user=.*/autologin-user=${KIOSK_USER}/" /etc/lightdm/lightdm.conf
elif [[ -d /etc/lightdm ]]; then
    cat > /etc/lightdm/lightdm.conf << LDMCONF
[Seat:*]
autologin-user=${KIOSK_USER}
autologin-user-timeout=0
LDMCONF
fi

echo ""
echo "✓ Kiosk setup complete!"
echo ""
echo "  Dashboard URL: ${DASHBOARD_URL}"
echo "  Auto-login user: ${KIOSK_USER}"
echo "  Kiosk script: ${KIOSK_SCRIPT}"
echo ""
echo "  Reboot the laptop to test: sudo reboot"
echo ""
echo "  To exit kiosk mode: Alt+F4 or Ctrl+Alt+T for terminal"
