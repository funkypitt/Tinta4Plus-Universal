#!/bin/bash
set -eE

# Tinta4PlusU Installer
# Installs either PyInstaller binaries or Python scripts into the system

APP_NAME="tinta4plusu"
INSTALL_DIR="/opt/tinta4plusu"
BIN_DIR="/usr/local/bin"
DESKTOP_DIR="/usr/share/applications"
AUTOSTART_DIR="/etc/xdg/autostart"
POLKIT_DIR="/usr/share/polkit-1/actions"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_MODE=""  # "binary" or "script"
LOG_FILE="/tmp/tinta4plusu-install.log"
CURRENT_STEP=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; echo "[INFO] $1" >> "$LOG_FILE"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; echo "[WARN] $1" >> "$LOG_FILE"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; echo "[ERROR] $1" >> "$LOG_FILE"; }

# ─── Error trap ──────────────────────────────────────────────────────────────

on_error() {
    local exit_code=$?
    local line_no=$1
    echo ""
    error "Installation failed at line ${line_no} (exit code ${exit_code})"
    if [ -n "$CURRENT_STEP" ]; then
        error "During step: ${CURRENT_STEP}"
    fi
    error "See full log: ${LOG_FILE}"
    echo ""
    error "You can retry the installation after fixing the issue."
    error "To clean up a partial install: sudo bash installer.sh --uninstall"
    exit "$exit_code"
}

trap 'on_error ${LINENO}' ERR

step() {
    CURRENT_STEP="$1"
    info "$1"
}

# ─── Uninstall ───────────────────────────────────────────────────────────────

do_uninstall() {
    info "Uninstalling Tinta4PlusU..."

    rm -f  "${BIN_DIR}/tinta4plusu"
    rm -f  "${BIN_DIR}/tinta4plusu-helper"
    rm -rf "${INSTALL_DIR}"
    rm -f  "${DESKTOP_DIR}/tinta4plusu.desktop"
    rm -f  "${AUTOSTART_DIR}/tinta4plusu-autostart.desktop"
    rm -f  "${POLKIT_DIR}/org.tinta4plusu.helper.policy"

    info "Tinta4PlusU has been uninstalled."
    exit 0
}

# ─── Choose install mode ────────────────────────────────────────────────────

choose_mode() {
    local has_binary=false
    if [ -f "${SCRIPT_DIR}/dist/tinta4plusu/tinta4plusu" ] && \
       [ -f "${SCRIPT_DIR}/dist/tinta4plusu-helper/tinta4plusu-helper" ]; then
        has_binary=true
    fi

    local has_script=false
    if [ -f "${SCRIPT_DIR}/Tinta4Plus.py" ] && \
       [ -f "${SCRIPT_DIR}/HelperDaemon.py" ]; then
        has_script=true
    fi

    if [ "$has_binary" = false ] && [ "$has_script" = false ]; then
        error "No installable files found."
        error "Either run 'bash build.sh' first (for binary mode) or ensure .py files are present."
        exit 1
    fi

    echo ""
    echo -e "${CYAN}─── Installation Mode ───${NC}"
    echo ""
    if [ "$has_binary" = true ]; then
        echo "  1) Compiled binary (PyInstaller)"
        echo "     Standalone executables, no Python needed at runtime."
    else
        echo -e "  1) Compiled binary ${YELLOW}[not available — run 'bash build.sh' first]${NC}"
    fi
    echo ""
    if [ "$has_script" = true ]; then
        echo "  2) Python scripts"
        echo "     Installs .py files directly. Requires Python 3 + dependencies at runtime."
        echo "     Easier to debug and modify."
    else
        echo -e "  2) Python scripts ${YELLOW}[not available — .py files not found]${NC}"
    fi
    echo ""

    while true; do
        read -rp "Choose installation mode [1/2]: " choice
        case "$choice" in
            1)
                if [ "$has_binary" = true ]; then
                    INSTALL_MODE="binary"
                    break
                else
                    error "Binaries not built. Run 'bash build.sh' first."
                fi
                ;;
            2)
                if [ "$has_script" = true ]; then
                    INSTALL_MODE="script"
                    break
                else
                    error "Python scripts not found."
                fi
                ;;
            *) error "Please enter 1 or 2." ;;
        esac
    done

    info "Installation mode: ${INSTALL_MODE}"
}

# ─── Check prerequisites ────────────────────────────────────────────────────

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        error "This installer must be run as root (sudo bash installer.sh)"
        exit 1
    fi
}

# ─── Detect desktop environment ─────────────────────────────────────────────

detect_de() {
    DE="unknown"

    # 1. Try env vars (work when using sudo -E, or running as normal user)
    local xdg="${XDG_CURRENT_DESKTOP:-}"
    local session="${DESKTOP_SESSION:-}"

    # 2. If empty (sudo strips env), read from the invoking user's session
    if [ -z "$xdg" ] && [ -n "$SUDO_USER" ]; then
        # Get the invoking user's active session via loginctl
        local uid
        uid=$(id -u "$SUDO_USER" 2>/dev/null) || true
        if [ -n "$uid" ]; then
            local sess_id
            sess_id=$(loginctl list-sessions --no-legend 2>/dev/null \
                      | awk -v u="$uid" '$2 == u {print $1; exit}') || true
            if [ -n "$sess_id" ]; then
                xdg=$(loginctl show-session "$sess_id" -p Desktop --value 2>/dev/null) || true
            fi
        fi
    fi

    # 3. Fallback: detect from running processes
    if [ -z "$xdg" ]; then
        if pgrep -x gnome-shell &>/dev/null; then
            xdg="GNOME"
        elif pgrep -x xfce4-session &>/dev/null; then
            xdg="XFCE"
        elif pgrep -x plasmashell &>/dev/null; then
            xdg="KDE"
        fi
    fi

    # 4. Map to our labels
    case "${xdg}${session}" in
        *GNOME*|*gnome*|*Unity*|*Budgie*|*ubuntu*) DE="gnome" ;;
        *XFCE*|*xfce*)                              DE="xfce" ;;
        *KDE*|*plasma*)                              DE="kde" ;;
    esac

    info "Detected desktop environment: ${DE}"
}

# ─── Install system dependencies ────────────────────────────────────────────

install_deps() {
    step "Installing system dependencies"

    # Common packages
    local pkgs="libusb-1.0-0"

    # Python scripts need the full Python stack
    if [ "$INSTALL_MODE" = "script" ]; then
        pkgs="$pkgs python3 python3-tk python3-usb"
    else
        pkgs="$pkgs python3-tk"
    fi

    case "$DE" in
        gnome)
            pkgs="$pkgs gnome-themes-extra"
            ;;
        xfce)
            pkgs="$pkgs xfce4-settings"
            ;;
    esac

    if ! apt-get update -qq >> "$LOG_FILE" 2>&1; then
        error "apt-get update failed. Check your internet connection and sources."
        error "See: ${LOG_FILE}"
        exit 1
    fi

    if ! apt-get install -y -qq $pkgs >> "$LOG_FILE" 2>&1; then
        error "apt-get install failed for packages: ${pkgs}"
        error "See: ${LOG_FILE}"
        exit 1
    fi
    info "APT packages installed: ${pkgs}"

    # Script mode: install pip packages not available in apt
    if [ "$INSTALL_MODE" = "script" ]; then
        step "Installing Python pip packages (portio, pyusb)"
        local pip_cmd="pip3"
        if ! command -v pip3 &>/dev/null; then
            apt-get install -y -qq python3-pip >> "$LOG_FILE" 2>&1
        fi
        # Install as system-wide (running as root)
        if $pip_cmd install --break-system-packages portio pyusb >> "$LOG_FILE" 2>&1; then
            info "pip packages installed."
        elif $pip_cmd install portio pyusb >> "$LOG_FILE" 2>&1; then
            info "pip packages installed."
        else
            warn "pip install failed for portio/pyusb."
            warn "You will need to run manually: pip3 install portio pyusb"
            warn "See: ${LOG_FILE}"
        fi
    fi
}

# ─── Verify dependencies ────────────────────────────────────────────────────

check_deps() {
    info "Verifying dependencies..."
    local missing=()

    # Check system commands/libs
    if ! ldconfig -p 2>/dev/null | grep -q libusb-1.0; then
        missing+=("libusb-1.0-0 (apt)")
    fi

    # Both modes need tkinter at runtime
    if ! python3 -c "import tkinter" 2>/dev/null; then
        missing+=("python3-tk (apt)")
    fi

    # Script mode needs Python modules
    if [ "$INSTALL_MODE" = "script" ]; then
        if ! command -v python3 &>/dev/null; then
            missing+=("python3 (apt)")
        else
            for mod in usb portio; do
                if ! python3 -c "import $mod" 2>/dev/null; then
                    case "$mod" in
                        usb)    missing+=("pyusb (pip3 install pyusb)") ;;
                        portio) missing+=("portio (pip3 install portio)") ;;
                    esac
                fi
            done
        fi
    fi

    # Check display tools
    if ! command -v feh &>/dev/null && ! command -v imv &>/dev/null; then
        warn "Neither 'feh' nor 'imv' found — privacy image display may not work."
        warn "Install one with: apt install feh"
    fi

    if [ ${#missing[@]} -eq 0 ]; then
        info "All dependencies OK."
    else
        echo ""
        error "Missing dependencies:"
        for dep in "${missing[@]}"; do
            echo -e "  ${RED}✗${NC} ${dep}"
        done
        echo ""
        warn "The application may not work correctly until these are installed."
    fi
}

# ─── Install: binary mode ───────────────────────────────────────────────────

install_binary() {
    step "Installing compiled binaries to ${INSTALL_DIR}"

    mkdir -p "${INSTALL_DIR}"

    info "Copying GUI bundle..."
    cp -r "${SCRIPT_DIR}/dist/tinta4plusu"        "${INSTALL_DIR}/"
    info "Copying helper bundle..."
    cp -r "${SCRIPT_DIR}/dist/tinta4plusu-helper"  "${INSTALL_DIR}/"

    chmod 755 "${INSTALL_DIR}/tinta4plusu/tinta4plusu"
    chmod 755 "${INSTALL_DIR}/tinta4plusu-helper/tinta4plusu-helper"

    ln -sf "${INSTALL_DIR}/tinta4plusu/tinta4plusu"              "${BIN_DIR}/tinta4plusu"
    ln -sf "${INSTALL_DIR}/tinta4plusu-helper/tinta4plusu-helper" "${BIN_DIR}/tinta4plusu-helper"

    # Verify binaries are executable
    if [ ! -x "${BIN_DIR}/tinta4plusu" ]; then
        error "GUI binary symlink is not executable: ${BIN_DIR}/tinta4plusu"
        exit 1
    fi
    if [ ! -x "${BIN_DIR}/tinta4plusu-helper" ]; then
        error "Helper binary symlink is not executable: ${BIN_DIR}/tinta4plusu-helper"
        exit 1
    fi

    info "Binaries installed and verified."
}

# ─── Install: script mode ───────────────────────────────────────────────────

install_script() {
    step "Installing Python scripts to ${INSTALL_DIR}"

    mkdir -p "${INSTALL_DIR}"

    # Copy all Python source files
    local py_files=(
        Tinta4Plus.py
        HelperDaemon.py
        DisplayManager.py
        ThemeManager.py
        HelperClient.py
        ECController.py
        EInkUSBController.py
        WatchdogTimer.py
    )

    local copy_failed=false
    for f in "${py_files[@]}"; do
        if [ ! -f "${SCRIPT_DIR}/${f}" ]; then
            error "Missing source file: ${f}"
            copy_failed=true
        else
            cp "${SCRIPT_DIR}/${f}" "${INSTALL_DIR}/"
        fi
    done
    if [ "$copy_failed" = true ]; then
        error "Some source files are missing. Installation cannot continue."
        exit 1
    fi

    # Copy EULA file
    if [ -f "${SCRIPT_DIR}/README_EULA_INSTRUCTIONS_WARNINGS.txt" ]; then
        cp "${SCRIPT_DIR}/README_EULA_INSTRUCTIONS_WARNINGS.txt" "${INSTALL_DIR}/"
        info "EULA file copied."
    else
        warn "EULA file not found — first-launch disclaimer will not work."
    fi

    # Copy privacy images
    local img_count=0
    for img in "${SCRIPT_DIR}"/eink-disable*.jpg; do
        [ -f "$img" ] && cp "$img" "${INSTALL_DIR}/" && img_count=$((img_count + 1))
    done
    if [ "$img_count" -eq 0 ]; then
        warn "No privacy images (eink-disable*.jpg) found — privacy screen will not work."
    else
        info "Copied ${img_count} privacy image(s)."
    fi

    chmod 755 "${INSTALL_DIR}/Tinta4Plus.py"
    chmod 755 "${INSTALL_DIR}/HelperDaemon.py"

    # Create launcher wrappers in /usr/local/bin
    cat > "${BIN_DIR}/tinta4plusu" << 'WRAPPER'
#!/bin/bash
exec python3 /opt/tinta4plusu/Tinta4Plus.py "$@"
WRAPPER
    chmod 755 "${BIN_DIR}/tinta4plusu"

    cat > "${BIN_DIR}/tinta4plusu-helper" << 'WRAPPER'
#!/bin/bash
exec python3 /opt/tinta4plusu/HelperDaemon.py "$@"
WRAPPER
    chmod 755 "${BIN_DIR}/tinta4plusu-helper"

    info "Python scripts installed."
}

# ─── Install desktop entries ────────────────────────────────────────────────

install_desktop() {
    step "Installing desktop entries"

    cp "${SCRIPT_DIR}/tinta4plusu.desktop"           "${DESKTOP_DIR}/"
    cp "${SCRIPT_DIR}/tinta4plusu-autostart.desktop"  "${AUTOSTART_DIR}/"

    # Validate desktop files if desktop-file-validate is available
    if command -v desktop-file-validate &>/dev/null; then
        desktop-file-validate "${DESKTOP_DIR}/tinta4plusu.desktop" 2>/dev/null || true
    fi

    # Update desktop database
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "${DESKTOP_DIR}" 2>/dev/null || true
    fi

    info "Desktop entries installed."
}

# ─── PolicyKit (optional) ───────────────────────────────────────────────────

install_polkit() {
    echo ""
    echo -e "${CYAN}─── PolicyKit Configuration ───${NC}"
    echo "Install a PolicyKit policy to avoid re-entering your password"
    echo "every time the helper daemon starts?"
    echo "(The first launch will still require authentication)"
    echo ""
    read -rp "Install PolicyKit policy? [y/N] " answer

    if [[ "$answer" =~ ^[Yy]$ ]]; then
        mkdir -p "${POLKIT_DIR}"
        cp "${SCRIPT_DIR}/org.tinta4plusu.helper.policy" "${POLKIT_DIR}/"
        info "PolicyKit policy installed."
    else
        info "Skipping PolicyKit policy."
    fi
}

# ─── Main ────────────────────────────────────────────────────────────────────

main() {
    # Initialize log file
    echo "=== Tinta4PlusU Installer — $(date) ===" > "$LOG_FILE"

    echo ""
    echo "╔══════════════════════════════════════╗"
    echo "║      Tinta4PlusU Installer           ║"
    echo "║  eInk Control for ThinkBook Plus G4  ║"
    echo "╚══════════════════════════════════════╝"
    echo ""

    # Handle --uninstall
    if [ "${1}" = "--uninstall" ]; then
        check_root
        do_uninstall
    fi

    check_root
    choose_mode
    detect_de
    install_deps

    if [ "$INSTALL_MODE" = "binary" ]; then
        install_binary
    else
        install_script
    fi

    install_desktop
    install_polkit
    check_deps

    echo ""
    info "════════════════════════════════════════"
    info " Installation complete! (mode: ${INSTALL_MODE})"
    info ""
    info " Launch from terminal:  tinta4plusu"
    info " Or find 'Tinta4PlusU' in your application menu."
    info " It will also autostart on next login."
    info ""
    info " To uninstall:  sudo bash installer.sh --uninstall"
    info " Install log:   ${LOG_FILE}"
    info "════════════════════════════════════════"
    echo ""
}

main "$@"
