import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import subprocess
import threading
import os
import json
import sys
import tempfile
import hashlib
import shutil
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
from datetime import datetime, timezone

from workspace import WorkspaceManager, WorkspaceError


# ============================================================
# SCHOOLHUB
# Central Workspace & Safe Git Sync
# ============================================================

LOCAL_APPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
APP_DIR = os.path.join(LOCAL_APPDATA, "SchoolHub")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_FILE = os.path.join(APP_DIR, "schoolhub.log")
SYNC_STATE_FILE = os.path.join(APP_DIR, "sync_state.json")

APP_VERSION = "2.5.0"
RELEASE_API = "https://api.github.com/repos/MeloniMirko/SchoolHub/releases/latest"
UPDATE_USER_AGENT = "SchoolHub-Updater/2.5.0"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_STAMP_FILE = os.path.join(APP_DIR, "last_update_check.txt")
GIT_TIMEOUT_SECONDS = 180
LFS_MEDIA_PATTERNS = ("*.mp3", "*.mp4", "*.m4a", "*.wav", "*.flac", "*.aac", "*.ogg", "*.mov", "*.mkv", "*.avi", "*.webm")
DEVICE_STATUS_BRANCH = "schoolhub-status"

DEFAULT_REPO = os.path.join(APP_DIR, "TempGit")
DEFAULT_REMOTE = ""
DEFAULT_BRANCH = "master"
DEFAULT_INTERVAL = 300

DEFAULT_WORKSPACE = os.path.join(APP_DIR, "Workspaces", "Scuola")


def bundled_git_info():
    """Return (git_executable, environment, bundled) for SchoolHub's Git runtime."""
    env = os.environ.copy()
    roots = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.append(os.path.join(mei, "vendor", "git"))
    # Also support a side-by-side development/runtime layout.
    roots.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor", "git"))

    for root in roots:
        root = os.path.abspath(root)
        git_exe = os.path.join(root, "cmd", "git.exe")
        if os.path.isfile(git_exe):
            vendor_root = os.path.dirname(root)
            gcm_root = os.path.join(vendor_root, "gcm")
            lfs_root = os.path.join(vendor_root, "lfs")
            gcm_exe = os.path.join(gcm_root, "git-credential-manager.exe")
            lfs_exe = os.path.join(lfs_root, "git-lfs.exe")
            path_parts = [
                os.path.join(root, "cmd"),
                os.path.join(root, "mingw64", "bin"),
                os.path.join(root, "usr", "bin"),
            ]
            git_config = []
            if os.path.isfile(gcm_exe):
                path_parts.append(gcm_root)
                env["GCM_INTERACTIVE"] = "always"
                env["GCM_GUI_PROMPT"] = "1"
                env["GIT_TERMINAL_PROMPT"] = "0"
                git_config.extend([
                    ("credential.helper", "manager"),
                    ("credential.https://github.com.useHttpPath", "true"),
                ])
            if os.path.isfile(lfs_exe):
                path_parts.append(lfs_root)
                # Configure LFS in-process so clone/add/push work without a
                # machine-wide Git installation or modifying the user's .gitconfig.
                git_config.extend([
                    ("filter.lfs.process", "git-lfs filter-process"),
                    ("filter.lfs.smudge", "git-lfs smudge -- %f"),
                    ("filter.lfs.clean", "git-lfs clean -- %f"),
                    ("filter.lfs.required", "true"),
                ])
            # Keep long school/media paths working in bundled Git for Windows.
            git_config.append(("core.longpaths", "true"))
            if git_config:
                env["GIT_CONFIG_COUNT"] = str(len(git_config))
                for idx, (key, value) in enumerate(git_config):
                    env[f"GIT_CONFIG_KEY_{idx}"] = key
                    env[f"GIT_CONFIG_VALUE_{idx}"] = value
            env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])
            env["GIT_EXEC_PATH"] = os.path.join(root, "mingw64", "libexec", "git-core")
            return git_exe, env, True

    system_git = shutil.which("git")
    if system_git:
        return system_git, env, False
    return None, env, False


# ============================================================
# COLORS
# ============================================================

BG = "#070b12"
PANEL = "#0d1520"
PANEL2 = "#121d2a"
PANEL3 = "#182536"
BORDER = "#26384e"

TEXT = "#f4f7fb"
MUTED = "#91a0b5"

GREEN = "#22c55e"
YELLOW = "#fbbf24"
RED = "#fb7185"
BLUE = "#2f81f7"
CYAN = "#38bdf8"


# ============================================================
# CONFIG
# ============================================================

def default_config():
    return {
        "version": 7,
        "interval": DEFAULT_INTERVAL,
        "auto_sync": False,
        "auto_start": False,
        "workspace_path": DEFAULT_WORKSPACE,
        "vault_path": os.path.join(APP_DIR, "Vaults", "Scuola.vault"),
        "remote": DEFAULT_REMOTE,
        "branch": DEFAULT_BRANCH,
        "github_user": "",
        "sync_state_file": os.path.join(APP_DIR, "SyncState", "Scuola.json"),
        "onboarding_complete": False,
        "device_id": "",
        "device_name": "",
    }

def load_config():
    os.makedirs(APP_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        config = default_config()
        save_config(config)
        return config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError("config.json non contiene un oggetto JSON")
        defaults = default_config()
        for key, value in defaults.items():
            config.setdefault(key, value)
        return config
    except Exception:
        # Never destroy an unreadable configuration: preserve it for diagnosis.
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".corrupt-" + stamp)
        except Exception:
            pass
        config = default_config()
        try:
            save_config(config)
        except Exception:
            pass
        return config

def save_config(config):
    os.makedirs(APP_DIR, exist_ok=True)

    temp_file = CONFIG_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

    os.replace(temp_file, CONFIG_FILE)



def resource_path(name):
    roots = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.extend([mei, os.path.join(mei, "assets")])
    here = os.path.dirname(os.path.abspath(__file__))
    roots.extend([here, os.path.join(here, "assets"), os.path.dirname(here)])
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def version_tuple(value):
    import re
    nums = re.findall(r"\d+", str(value))
    return tuple(int(x) for x in (nums[:3] + ["0", "0", "0"])[:3])


class PasswordDialog:
    """Modern modal password dialog with show/hide and inline validation."""
    def __init__(self, parent, title, subtitle, confirm=False, min_length=1):
        self.parent = parent
        self.result = None
        self.confirm = confirm
        self.min_length = min_length
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)

        w, h = 470, (360 if confirm else 305)
        parent.update_idletasks()
        x = parent.winfo_rootx() + max(20, (parent.winfo_width() - w)//2)
        y = parent.winfo_rooty() + max(20, (parent.winfo_height() - h)//2)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

        outer = tk.Frame(self.win, bg=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=22)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", pady=(0, 16))
        tk.Label(head, text="◈", font=("Segoe UI Symbol", 30, "bold"), fg=CYAN, bg=BG).pack(side="left", padx=(0,14))
        textcol = tk.Frame(head, bg=BG); textcol.pack(side="left", fill="x", expand=True)
        tk.Label(textcol, text=title, font=("Segoe UI", 17, "bold"), fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(textcol, text=subtitle, font=("Segoe UI", 9), fg=MUTED, bg=BG, justify="left", wraplength=360).pack(anchor="w", pady=(4,0))

        card = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x")
        tk.Label(card, text="PASSWORD", font=("Segoe UI", 8, "bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=16, pady=(14,5))
        row = tk.Frame(card, bg=PANEL); row.pack(fill="x", padx=14, pady=(0,10))
        self.entry = tk.Entry(row, show="•", bg=PANEL2, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=10)
        self.showing = False
        self.show_btn = tk.Button(row, text="MOSTRA", command=self.toggle_show, bg=PANEL3, fg=MUTED, activebackground=PANEL3, activeforeground=TEXT, relief="flat", bd=0, font=("Segoe UI",8,"bold"), cursor="hand2", padx=11, pady=8)
        self.show_btn.pack(side="left", padx=(8,0))

        self.confirm_entry = None
        if confirm:
            tk.Label(card, text="CONFERMA PASSWORD", font=("Segoe UI", 8, "bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=16, pady=(2,5))
            self.confirm_entry = tk.Entry(card, show="•", bg=PANEL2, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI",11))
            self.confirm_entry.pack(fill="x", padx=14, pady=(0,12), ipady=10)

        self.error = tk.Label(outer, text="", font=("Segoe UI", 8), fg=RED, bg=BG)
        self.error.pack(anchor="w", pady=(8,0))
        actions = tk.Frame(outer, bg=BG); actions.pack(fill="x", side="bottom")
        tk.Button(actions, text="ANNULLA", command=self.cancel, bg=PANEL2, fg=MUTED, activebackground=PANEL3, activeforeground=TEXT, relief="flat", bd=0, font=("Segoe UI",9,"bold"), cursor="hand2", padx=18, pady=10).pack(side="right")
        tk.Button(actions, text="CONTINUA", command=self.accept, bg=BLUE, fg="white", activebackground=BLUE, activeforeground="white", relief="flat", bd=0, font=("Segoe UI",9,"bold"), cursor="hand2", padx=20, pady=10).pack(side="right", padx=(0,8))

        self.entry.bind("<Return>", lambda e: self.accept())
        self.entry.bind("<Escape>", lambda e: self.cancel())
        if self.confirm_entry:
            self.confirm_entry.bind("<Return>", lambda e: self.accept())
            self.confirm_entry.bind("<Escape>", lambda e: self.cancel())
        self.entry.focus_set()
        parent.wait_window(self.win)

    def toggle_show(self):
        self.showing = not self.showing
        char = "" if self.showing else "•"
        self.entry.configure(show=char)
        if self.confirm_entry:
            self.confirm_entry.configure(show=char)
        self.show_btn.configure(text="NASCONDI" if self.showing else "MOSTRA")

    def accept(self):
        value = self.entry.get()
        if len(value) < self.min_length:
            self.error.configure(text=f"La password deve contenere almeno {self.min_length} caratteri.")
            return
        if self.confirm_entry is not None and value != self.confirm_entry.get():
            self.error.configure(text="Le due password non coincidono.")
            return
        self.result = value
        self.win.grab_release()
        self.win.destroy()

    def cancel(self):
        self.result = None
        try: self.win.grab_release()
        except Exception: pass
        self.win.destroy()


class ProgressDialog:
    """Dedicated operation window with a real determinate progress bar."""
    def __init__(self, parent, title, subtitle="Operazione in corso"):
        self.parent = parent
        self.started = time.monotonic()
        self.closed = False
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        w, h = 560, 270
        parent.update_idletasks()
        x = parent.winfo_rootx() + max(20, (parent.winfo_width()-w)//2)
        y = parent.winfo_rooty() + max(20, (parent.winfo_height()-h)//2)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

        shell = tk.Frame(self.win, bg=BG); shell.pack(fill="both", expand=True, padx=28, pady=24)
        tk.Label(shell, text=title, font=("Segoe UI",18,"bold"), fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(shell, text=subtitle, font=("Segoe UI",9), fg=MUTED, bg=BG).pack(anchor="w", pady=(4,18))
        self.stage = tk.Label(shell, text="Preparazione…", font=("Segoe UI",11,"bold"), fg=CYAN, bg=BG)
        self.stage.pack(anchor="w")
        self.detail = tk.Label(shell, text="", font=("Segoe UI",9), fg=MUTED, bg=BG, anchor="w", justify="left", wraplength=500)
        self.detail.pack(fill="x", pady=(5,13))
        style = ttk.Style(self.win)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("SchoolHub.Horizontal.TProgressbar", troughcolor=PANEL2, background=BLUE, bordercolor=PANEL2, lightcolor=BLUE, darkcolor=BLUE, thickness=18)
        self.bar = ttk.Progressbar(shell, style="SchoolHub.Horizontal.TProgressbar", orient="horizontal", mode="determinate", maximum=100, value=0)
        self.bar.pack(fill="x")
        meta = tk.Frame(shell,bg=BG); meta.pack(fill="x", pady=(9,0))
        self.percent = tk.Label(meta, text="0%", font=("Segoe UI",9,"bold"), fg=TEXT, bg=BG); self.percent.pack(side="left")
        self.elapsed = tk.Label(meta, text="00:00", font=("Consolas",9), fg=MUTED, bg=BG); self.elapsed.pack(side="right")
        tk.Label(shell, text="Puoi continuare a usare Windows. Non chiudere SchoolHub durante la cifratura o la sincronizzazione.", font=("Segoe UI",8), fg=MUTED, bg=BG, wraplength=500, justify="left").pack(anchor="w", pady=(15,0))
        self.tick()

    def tick(self):
        if self.closed or not self.win.winfo_exists(): return
        sec=int(time.monotonic()-self.started)
        self.elapsed.configure(text=f"{sec//60:02d}:{sec%60:02d}")
        self.win.after(1000,self.tick)

    def set(self, value, stage=None, detail=None):
        if self.closed or not self.win.winfo_exists(): return
        value=max(0,min(100,float(value)))
        self.bar["value"]=value
        self.percent.configure(text=f"{int(value)}%")
        if stage is not None: self.stage.configure(text=stage)
        if detail is not None: self.detail.configure(text=detail)
        self.win.update_idletasks()

    def close(self):
        self.closed=True
        try: self.win.destroy()
        except Exception: pass


# ============================================================
# APPLICATION
# ============================================================

class SchoolHub:

    def __init__(self, root):

        self.root = root
        self.config = load_config()
        self._ensure_vault_config()

        try:
            self.interval = int(self.config.get("interval", DEFAULT_INTERVAL))
        except Exception:
            self.interval = DEFAULT_INTERVAL
        self.interval = max(30, self.interval)
        self.auto_sync_enabled = bool(self.config.get("auto_sync", False))
        self.auto_start_enabled = bool(self.config.get("auto_start", False))
        self.auto_sync_job = None
        self.auto_sync_pending_unlock = False
        self.repo_path = "Repository Git temporaneo (eliminato al termine della sync)"
        self._load_active_vault()

        self.running = True
        self.sync_running = False
        self.workspace_busy = False
        self.last_conflicts = []
        self.progress_dialog = None
        self.update_check_running = False

        # UltraLight: apply the startup policy immediately so upgrades from
        # older builds do not keep a stale Windows autorun entry.
        self.set_windows_startup(self.auto_start_enabled)

        # Serve a invalidare callback provenienti da vecchie pagine
        self.page_generation = 0

        self.root.title(f"SchoolHub {APP_VERSION}")
        self.root.geometry("1180x760")
        self.root.minsize(980, 650)
        self.root.configure(bg=BG)
        icon_path = resource_path("schoolhub_icon.png")
        if icon_path:
            try:
                self._app_icon = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self._app_icon)
            except Exception:
                self._app_icon = None

        self.build_ui()

        self.navigate("Home")

        if not self.onboarding_complete and not self.remote:
            self.root.after(500, lambda: self.show_github_setup(first_run=True))

        self.root.after(60000, self.update_clock)

        self.root.after(
            1500,
            self.initial_status
        )

        # Silent update check shortly after startup. Updates are downloaded only
        # after explicit confirmation and are verified with SHA-256.
        self.root.after(4500, lambda: self.check_for_updates(silent=True))

        if self.auto_sync_enabled and self.git_enabled:
            # Primo controllo poco dopo l'avvio, poi intervallo configurato.
            self.root.after(2000, self.auto_sync)

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.close
        )

    # ========================================================
    # SINGLE-VAULT CONFIG / MIGRATION
    # ========================================================

    def _ensure_vault_config(self):
        """Normalize historical layouts to one Scuola Vault without deleting user data."""
        old = self.config if isinstance(self.config, dict) else {}
        old_vaults = old.get("vaults") if isinstance(old.get("vaults"), dict) else {}
        scuola = old_vaults.get("Scuola") if isinstance(old_vaults.get("Scuola"), dict) else {}

        canonical_wp = os.path.normpath(DEFAULT_WORKSPACE)
        canonical_vp = os.path.normpath(os.path.join(APP_DIR, "Vaults", "Scuola.vault"))
        canonical_state = os.path.normpath(os.path.join(APP_DIR, "SyncState", "Scuola.json"))
        os.makedirs(os.path.dirname(canonical_wp), exist_ok=True)
        os.makedirs(os.path.dirname(canonical_vp), exist_ok=True)
        os.makedirs(os.path.dirname(canonical_state), exist_ok=True)

        old_wp = scuola.get("workspace_path") or old.get("workspace_path") or os.path.join(APP_DIR, "Workspace")
        old_vp = scuola.get("vault_path") or old.get("vault_path") or (str(old_wp) + ".vault")
        old_state = scuola.get("sync_state_file") or old.get("sync_state_file") or os.path.join(APP_DIR, "sync_state.json")

        def same(a, b):
            try: return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
            except Exception: return False

        def vault_score(path):
            try:
                meta = os.path.join(path, "vault.json")
                files = os.path.join(path, "files")
                if not (os.path.isfile(meta) and os.path.isdir(files)):
                    return -1
                count = 0
                for _, _, fs in os.walk(files): count += len(fs)
                return count
            except Exception:
                return -1

        vault_candidates = []
        for candidate in [
            canonical_vp, old_vp,
            os.path.join(APP_DIR, "Workspace.vault"),
            os.path.join(APP_DIR, "Vault", "Scuola.vault"),
            os.path.join(APP_DIR, "Vaults", "Scuola.vault"),
        ]:
            if candidate and not any(same(candidate, x) for x in vault_candidates):
                vault_candidates.append(os.path.normpath(candidate))

        canonical_score = vault_score(canonical_vp)
        best = max(vault_candidates, key=vault_score) if vault_candidates else canonical_vp
        best_score = vault_score(best)
        chosen_vp = canonical_vp
        # Keep a populated canonical Vault. Replace only an absent/invalid/empty one
        # when a historical valid Vault contains actual encrypted files.
        should_migrate_vault = (canonical_score < 0 and best_score >= 0) or (canonical_score == 0 and best_score > 0)
        if should_migrate_vault and not same(best, canonical_vp):
            try:
                if os.path.exists(canonical_vp):
                    backup = canonical_vp + ".migration-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                    shutil.move(canonical_vp, backup)
                shutil.move(best, canonical_vp)
            except Exception:
                # If Windows blocks migration, use the valid old location rather than
                # pretending the Vault vanished.
                chosen_vp = best

        def file_count(path):
            try:
                if not os.path.isdir(path): return -1
                return sum(len(fs) for _, _, fs in os.walk(path))
            except Exception: return -1

        workspace_candidates = []
        for candidate in [canonical_wp, old_wp, os.path.join(APP_DIR, "Workspace")]:
            if candidate and not any(same(candidate, x) for x in workspace_candidates):
                workspace_candidates.append(os.path.normpath(candidate))
        chosen_wp = canonical_wp
        canonical_wc = file_count(canonical_wp)
        best_wp = max(workspace_candidates, key=file_count) if workspace_candidates else canonical_wp
        best_wc = file_count(best_wp)
        if ((canonical_wc < 0 and best_wc >= 0) or (canonical_wc == 0 and best_wc > 0)) and not same(best_wp, canonical_wp):
            try:
                if os.path.isdir(canonical_wp) and not os.listdir(canonical_wp):
                    os.rmdir(canonical_wp)
                shutil.move(best_wp, canonical_wp)
            except Exception:
                chosen_wp = best_wp

        # Preserve the old baseline if present; do not invent one.
        if not os.path.exists(canonical_state) and old_state and os.path.isfile(old_state) and not same(old_state, canonical_state):
            try:
                shutil.copy2(old_state, canonical_state)
            except Exception:
                pass

        old_version = int(old.get("version", 0) or 0) if str(old.get("version", "0")).isdigit() else 0
        # Preserve the repository explicitly saved by the user, including
        # MeloniMirko/Scuola. DEFAULT_REMOTE is empty, so there is no public
        # repository that can appear here unless the user configured it.
        remote = scuola.get("remote") or old.get("remote") or DEFAULT_REMOTE
        branch = scuola.get("branch") or old.get("branch") or DEFAULT_BRANCH
        github_user = scuola.get("github_user") or old.get("github_user") or ""
        if not github_user:
            import re
            m = re.match(r"https?://github\.com/([^/]+)/", str(remote))
            github_user = m.group(1) if m else ""
        try:
            interval = int(old.get("interval", DEFAULT_INTERVAL))
        except Exception:
            interval = DEFAULT_INTERVAL
        interval = max(30, min(interval, 86400))
        migrated_auto_sync = bool(old.get("auto_sync", False)) if old_version >= 7 else False
        migrated_auto_start = bool(old.get("auto_start", False)) if old_version >= 7 else False
        if not remote:
            migrated_auto_sync = False

        self.config = {
            "version": 7,
            "interval": interval,
            "auto_sync": migrated_auto_sync,
            "auto_start": migrated_auto_start,
            "workspace_path": chosen_wp,
            "vault_path": chosen_vp,
            "remote": remote,
            "branch": branch,
            "github_user": github_user,
            "sync_state_file": canonical_state,
            "onboarding_complete": bool(old.get("onboarding_complete", bool(remote))),
            "device_id": str(old.get("device_id") or uuid.uuid4().hex),
            "device_name": str(old.get("device_name") or ""),
        }
        if not self.config["device_name"]:
            self.config["device_name"] = "Dispositivo " + self.config["device_id"][:6].upper()
        save_config(self.config)

    def _load_active_vault(self):
        self.workspace_path = os.path.normpath(self.config["workspace_path"])
        self.vault_path = os.path.normpath(self.config["vault_path"])
        self.remote = (self.config.get("remote", DEFAULT_REMOTE) or "").strip()
        self.git_enabled = bool(self.remote)
        self.branch = self.config.get("branch", DEFAULT_BRANCH)
        self.github_user = self.config.get("github_user", "")
        self.sync_state_file = self.config.get("sync_state_file", os.path.join(APP_DIR, "SyncState", "Scuola.json"))
        self.onboarding_complete = bool(self.config.get("onboarding_complete", False))
        self.device_id = str(self.config.get("device_id") or uuid.uuid4().hex)
        self.device_name = str(self.config.get("device_name") or ("Dispositivo " + self.device_id[:6].upper()))
        self.workspace = WorkspaceManager(self.workspace_path, self.vault_path)

    def ask_password(self, title, subtitle, confirm=False, min_length=1):
        dialog = PasswordDialog(self.root, title, subtitle, confirm=confirm, min_length=min_length)
        return dialog.result

    def open_progress(self, title, subtitle="Operazione in corso"):
        def create():
            if self.progress_dialog is not None:
                try: self.progress_dialog.close()
                except Exception: pass
            self.progress_dialog = ProgressDialog(self.root, title, subtitle)
        if threading.current_thread() is threading.main_thread():
            create()
        else:
            self.root.after(0, create)

    def progress_update(self, percent, stage=None, detail=None):
        def update():
            if self.progress_dialog is not None:
                try: self.progress_dialog.set(percent, stage, detail)
                except Exception: pass
        if self.running:
            self.root.after(0, update)

    def close_progress(self):
        def closeit():
            if self.progress_dialog is not None:
                try: self.progress_dialog.close()
                except Exception: pass
                self.progress_dialog = None
        if self.running:
            self.root.after(0, closeit)

    def workspace_progress_callback(self, start=0, end=100):
        span = float(end) - float(start)
        def cb(fraction, phase, detail):
            self.progress_update(float(start) + span * float(fraction), phase, detail)
        return cb

    def _release_request(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UPDATE_USER_AGENT, "Accept": "application/vnd.github+json"})
        return urllib.request.urlopen(req, timeout=20)

    def check_for_updates(self, silent=False):
        if self.update_check_running or not self.running:
            return

        # UltraLight: silent startup checks may touch the network at most once
        # per day. Manual checks always bypass this cache.
        if silent:
            try:
                if os.path.isfile(UPDATE_STAMP_FILE):
                    age = time.time() - os.path.getmtime(UPDATE_STAMP_FILE)
                    if 0 <= age < UPDATE_CHECK_INTERVAL_SECONDS:
                        return
                os.makedirs(APP_DIR, exist_ok=True)
                with open(UPDATE_STAMP_FILE, "w", encoding="ascii") as fh:
                    fh.write(str(int(time.time())))
            except OSError:
                pass

        self.update_check_running = True
        def worker():
            try:
                with self._release_request(RELEASE_API) as response:
                    release = json.loads(response.read().decode("utf-8"))
                latest = release.get("tag_name") or release.get("name") or ""
                if version_tuple(latest) <= version_tuple(APP_VERSION):
                    if not silent and self.running:
                        self.root.after(0, lambda: messagebox.showinfo("Aggiornamenti", f"SchoolHub {APP_VERSION} è già aggiornato.", parent=self.root))
                    return
                if self.running:
                    self.root.after(0, lambda r=release: self._offer_update(r))
            except Exception as exc:
                self.write_log(f"⚠ Controllo aggiornamenti non riuscito: {exc}")
                if not silent and self.running:
                    self.root.after(0, lambda text=str(exc): messagebox.showwarning("Aggiornamenti", f"Impossibile controllare gli aggiornamenti.\n\n{text}", parent=self.root))
            finally:
                self.update_check_running = False
        threading.Thread(target=worker, daemon=True).start()

    def _offer_update(self, release):
        latest = release.get("tag_name") or release.get("name") or "nuova versione"
        if messagebox.askyesno(
            "Aggiornamento SchoolHub",
            f"È disponibile {latest}.\n\nVersione installata: {APP_VERSION}\n\nScaricare e installare adesso?\nSchoolHub si riavvierà automaticamente.",
            parent=self.root,
        ):
            self._download_update(release)

    def _download_update(self, release):
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("Aggiornamenti", "L'aggiornamento automatico è disponibile nella versione EXE di SchoolHub.", parent=self.root)
            return
        assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", []) if a.get("name")}
        exe_url = assets.get("SchoolHub.exe")
        sha_url = assets.get("SchoolHub.exe.sha256.txt")
        if not exe_url or not sha_url:
            messagebox.showerror("Aggiornamenti", "La release non contiene SchoolHub.exe e il checksum richiesto.", parent=self.root)
            return
        self.open_progress("Aggiornamento SchoolHub", "Download e verifica della nuova versione")
        self.progress_update(5, "Connessione", "Controllo della release GitHub")
        def worker():
            try:
                update_dir = os.path.join(APP_DIR, "Updates")
                os.makedirs(update_dir, exist_ok=True)
                new_exe = os.path.join(update_dir, "SchoolHub.new.exe")
                self.progress_update(12, "Download", "Scaricamento SchoolHub.exe")
                req = urllib.request.Request(exe_url, headers={"User-Agent": UPDATE_USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as src, open(new_exe, "wb") as dst:
                    total = int(src.headers.get("Content-Length") or 0)
                    done = 0
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk: break
                        dst.write(chunk); done += len(chunk)
                        if total:
                            self.progress_update(12 + 68 * (done / total), "Download", f"{done/1024/1024:.0f} / {total/1024/1024:.0f} MB")
                self.progress_update(82, "Verifica", "Controllo SHA-256")
                with self._release_request(sha_url) as response:
                    sha_text = response.read().decode("utf-8", errors="replace").strip()
                expected = sha_text.split()[0].lower()
                h = hashlib.sha256()
                with open(new_exe, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                actual = h.hexdigest().lower()
                if len(expected) != 64 or actual != expected:
                    raise RuntimeError("Checksum SHA-256 della nuova versione non valido.")
                self.progress_update(95, "Installazione", "Preparazione del riavvio")
                current = os.path.abspath(sys.executable)
                batch = os.path.join(tempfile.gettempdir(), f"SchoolHub-update-{os.getpid()}.cmd")
                script = (
                    "@echo off\r\n"
                    "setlocal DisableDelayedExpansion\r\n"
                    # PyInstaller one-file children inherit internal _PYI_* state.
                    # A freshly replaced EXE must start as a brand-new application,
                    # otherwise it can look for python*.dll in the old _MEI folder.
                    "set PYINSTALLER_RESET_ENVIRONMENT=1\r\n"
                    f"set \"SCHOOLHUB_OLD_PID={os.getpid()}\"\r\n"
                    f"set \"SCHOOLHUB_NEW_EXE={new_exe}\"\r\n"
                    f"set \"SCHOOLHUB_TARGET={current}\"\r\n"
                    ":wait_old_process\r\n"
                    "tasklist /FI \"PID eq %SCHOOLHUB_OLD_PID%\" /NH 2>nul | findstr /R /C:\"^[^I].*%SCHOOLHUB_OLD_PID%\" >nul\r\n"
                    "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait_old_process)\r\n"
                    ":retry_replace\r\n"
                    "copy /b /y \"%SCHOOLHUB_NEW_EXE%\" \"%SCHOOLHUB_TARGET%.updating\" >nul 2>&1\r\n"
                    "if errorlevel 1 (timeout /t 1 /nobreak >nul & goto retry_replace)\r\n"
                    "move /y \"%SCHOOLHUB_TARGET%.updating\" \"%SCHOOLHUB_TARGET%\" >nul 2>&1\r\n"
                    "if errorlevel 1 (timeout /t 1 /nobreak >nul & goto retry_replace)\r\n"
                    "del /q \"%SCHOOLHUB_NEW_EXE%\" >nul 2>&1\r\n"
                    "start \"\" /D \"%~dp0\" \"%SCHOOLHUB_TARGET%\"\r\n"
                    "del \"%~f0\"\r\n"
                )
                with open(batch, "w", encoding="utf-8", newline="") as fh:
                    fh.write(script)
                self.progress_update(100, "Pronto", "SchoolHub verrà riavviato")
                time.sleep(0.4)
                subprocess.Popen(["cmd.exe", "/c", batch], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.root.after(0, self._close_for_update)
            except Exception as exc:
                self.write_log(f"✕ Aggiornamento fallito: {exc}")
                self.close_progress()
                if self.running:
                    self.root.after(0, lambda text=str(exc): messagebox.showerror("Aggiornamento", text, parent=self.root))
        threading.Thread(target=worker, daemon=True).start()

    def _close_for_update(self):
        self.running = False
        try: self.root.destroy()
        except Exception: pass

    # ========================================================
    # UI
    # ========================================================

    def build_ui(self):

        # ----------------------------------------------------
        # SIDEBAR
        # ----------------------------------------------------

        self.sidebar = tk.Frame(
            self.root,
            bg=PANEL,
            width=225
        )

        self.sidebar.pack(
            side="left",
            fill="y"
        )

        self.sidebar.pack_propagate(False)

        logo = tk.Frame(
            self.sidebar,
            bg=PANEL
        )

        logo.pack(
            fill="x",
            padx=20,
            pady=(22, 26)
        )

        icon_path = resource_path("schoolhub_icon.png")
        if icon_path:
            try:
                self.sidebar_icon = tk.PhotoImage(file=icon_path).subsample(12, 12)
                tk.Label(logo, image=self.sidebar_icon, bg=PANEL, bd=0).pack(anchor="w", pady=(0, 10))
            except Exception:
                self.sidebar_icon = None

        tk.Label(
            logo,
            text="SCHOOLHUB",
            font=("Segoe UI", 22, "bold"),
            fg=TEXT,
            bg=PANEL
        ).pack(anchor="w")

        tk.Label(
            logo,
            text="SECURE SCHOOL WORKSPACE",
            font=("Segoe UI", 8, "bold"),
            fg=CYAN,
            bg=PANEL
        ).pack(anchor="w", pady=(2,0))

        self.nav_buttons = {}

        self.add_nav("⌂", "Home")
        self.add_nav("⇅", "Sync")
        self.add_nav("🔒", "Workspace")
        self.add_nav("⚠", "Conflitti")
        self.add_nav("◷", "Attività")
        self.add_nav("⌘", "Dispositivi")
        self.add_nav("⚙", "Impostazioni")

        bottom = tk.Frame(
            self.sidebar,
            bg=PANEL
        )

        bottom.pack(
            side="bottom",
            fill="x",
            padx=20,
            pady=22
        )

        tk.Frame(
            bottom,
            bg=BORDER,
            height=1
        ).pack(
            fill="x",
            pady=(0, 15)
        )

        self.online_dot = tk.Label(
            bottom,
            text="●",
            font=("Segoe UI", 11),
            fg=GREEN,
            bg=PANEL
        )

        self.online_dot.pack(side="left")

        self.online_label = tk.Label(
            bottom,
            text=" GitHub",
            font=("Segoe UI", 9, "bold"),
            fg=MUTED,
            bg=PANEL
        )

        self.online_label.pack(side="left")

        tk.Label(
            bottom,
            text=f"v{APP_VERSION}",
            font=("Consolas", 8, "bold"),
            fg=MUTED,
            bg=PANEL
        ).pack(side="right")

        # ----------------------------------------------------
        # MAIN
        # ----------------------------------------------------

        self.main = tk.Frame(
            self.root,
            bg=BG
        )

        self.main.pack(
            side="left",
            fill="both",
            expand=True
        )

        self.header = tk.Frame(
            self.main,
            bg=BG,
            height=78
        )

        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.page_title = tk.Label(
            self.header,
            text="Home",
            font=("Segoe UI", 24, "bold"),
            fg=TEXT,
            bg=BG
        )

        self.page_title.pack(
            side="left",
            padx=32,
            pady=22
        )

        self.time_label = tk.Label(
            self.header,
            text="",
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=BG
        )

        self.time_label.pack(
            side="right",
            padx=32
        )

        self.content = tk.Frame(
            self.main,
            bg=BG
        )

        self.content.pack(
            fill="both",
            expand=True,
            padx=32,
            pady=(0, 25)
        )

    # ========================================================
    # NAVIGATION
    # ========================================================

    def add_nav(self, icon, name):

        button = tk.Button(
            self.sidebar,
            text=f"  {icon}   {name}",
            command=lambda n=name: self.navigate(n),
            anchor="w",
            relief="flat",
            bd=0,
            bg=PANEL,
            fg=MUTED,
            activebackground=PANEL2,
            activeforeground=TEXT,
            font=("Segoe UI", 11),
            cursor="hand2",
            padx=18,
            pady=12
        )

        button.pack(
            fill="x",
            padx=12,
            pady=2
        )

        self.nav_buttons[name] = button

    def navigate(self, page):

        if not self.running:
            return

        self.page_generation += 1

        for name, button in self.nav_buttons.items():

            if name == page:
                button.configure(
                    bg=PANEL2,
                    fg=TEXT
                )
            else:
                button.configure(
                    bg=PANEL,
                    fg=MUTED
                )

        self.page_title.configure(
            text=page
        )

        if page == "Home":
            self.show_home()

        elif page == "Sync":
            self.show_sync()

        elif page == "Workspace":
            self.show_workspace()

        elif page == "Conflitti":
            self.show_conflicts()

        elif page == "Attività":
            self.show_activity()

        elif page == "Dispositivi":
            self.show_devices()

        elif page == "Impostazioni":
            self.show_settings()

    def clear_content(self):

        self.page_generation += 1

        for widget in self.content.winfo_children():
            try:
                widget.destroy()
            except tk.TclError:
                pass

        # Eliminiamo i riferimenti alle vecchie pagine
        self.local_value = None
        self.github_value = None
        self.last_value = None

        self.status_dot = None
        self.status_text = None
        self.status_detail = None

        self.repo_label = None
        self.activity_box = None

    # ========================================================
    # WIDGET CHECK
    # ========================================================

    def widget_alive(self, widget):

        if widget is None:
            return False

        try:
            return bool(widget.winfo_exists())

        except tk.TclError:
            return False

    # ========================================================
    # HOME
    # ========================================================

    def show_home(self):

        self.clear_content()

        status = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        status.pack(
            fill="x",
            pady=(5, 18)
        )

        left = tk.Frame(
            status,
            bg=PANEL
        )

        left.pack(
            side="left",
            padx=25,
            pady=22
        )

        self.status_dot = tk.Label(
            left,
            text="●",
            font=("Segoe UI", 28),
            fg=GREEN,
            bg=PANEL
        )

        self.status_dot.pack(
            side="left",
            padx=(0, 16)
        )

        text = tk.Frame(
            left,
            bg=PANEL
        )

        text.pack(side="left")

        tk.Label(
            text,
            text="WORKSPACE",
            font=("Segoe UI", 9, "bold"),
            fg=MUTED,
            bg=PANEL
        ).pack(anchor="w")

        self.status_text = tk.Label(
            text,
            text="CONTROLLO...",
            font=("Segoe UI", 18, "bold"),
            fg=YELLOW,
            bg=PANEL
        )

        self.status_text.pack(anchor="w")

        self.status_detail = tk.Label(
            text,
            text="",
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=PANEL
        )

        self.status_detail.pack(anchor="w")

        tk.Button(
            status,
            text="SINCRONIZZA ORA",
            command=self.start_sync,
            bg=BLUE,
            fg="white",
            activebackground=BLUE,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            padx=20,
            pady=11
        ).pack(
            side="right",
            padx=25
        )

        # ----------------------------------------------------
        # COUNTERS
        # ----------------------------------------------------

        counters = tk.Frame(
            self.content,
            bg=BG
        )

        counters.pack(
            fill="x",
            pady=(0, 18)
        )

        self.local_value = self.counter_card(
            counters,
            "LOCALE",
            "0",
            "file da inviare"
        )

        self.github_value = self.counter_card(
            counters,
            "GITHUB",
            "0",
            "file da scaricare"
        )

        self.last_value = self.counter_card(
            counters,
            "ULTIMO CONTROLLO",
            "--:--:--",
            "sincronizzazione"
        )

        # ----------------------------------------------------
        # REPOSITORY
        # ----------------------------------------------------

        repo = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        repo.pack(
            fill="x",
            pady=(0, 18)
        )

        tk.Label(
            repo,
            text="REPOSITORY LOCALE",
            font=("Segoe UI", 9, "bold"),
            fg=MUTED,
            bg=PANEL
        ).pack(
            anchor="w",
            padx=20,
            pady=(15, 3)
        )

        self.repo_label = tk.Label(
            repo,
            text=self.repo_path,
            font=("Consolas", 9),
            fg=TEXT,
            bg=PANEL
        )

        self.repo_label.pack(
            anchor="w",
            padx=20,
            pady=(0, 15)
        )

        # ----------------------------------------------------
        # WORKSPACE
        # ----------------------------------------------------

        ws = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        ws.pack(
            fill="x",
            pady=(0, 18)
        )

        ws_unlocked = self.workspace.is_unlocked

        tk.Label(
            ws,
            text="WORKSPACE PROTETTO",
            font=("Segoe UI", 9, "bold"),
            fg=MUTED,
            bg=PANEL
        ).pack(
            side="left",
            padx=(20, 10),
            pady=15
        )

        tk.Label(
            ws,
            text="🔓 SBLOCCATO"
            if ws_unlocked
            else "🔒 BLOCCATO",
            font=("Segoe UI", 10, "bold"),
            fg=GREEN if ws_unlocked else YELLOW,
            bg=PANEL
        ).pack(
            side="left"
        )

        tk.Button(
            ws,
            text="GESTISCI",
            command=lambda: self.navigate("Workspace"),
            bg=PANEL2,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=15,
            pady=7
        ).pack(
            side="right",
            padx=20
        )

        # ----------------------------------------------------
        # ACTIVITY
        # ----------------------------------------------------

        activity = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        activity.pack(
            fill="both",
            expand=True
        )

        tk.Label(
            activity,
            text="ATTIVITÀ RECENTI",
            font=("Segoe UI", 10, "bold"),
            fg=TEXT,
            bg=PANEL
        ).pack(
            anchor="w",
            padx=22,
            pady=(18, 10)
        )

        self.activity_box = tk.Text(
            activity,
            bg=PANEL,
            fg=MUTED,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            font=("Consolas", 9),
            wrap="word"
        )

        self.activity_box.pack(
            fill="both",
            expand=True,
            padx=22,
            pady=(0, 20)
        )

        self.activity_box.configure(
            state="disabled"
        )

        self.load_recent_log()

    # ========================================================
    # COUNTER
    # ========================================================

    def counter_card(
        self,
        parent,
        title,
        value,
        subtitle
    ):

        card = tk.Frame(
            parent,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        card.pack(
            side="left",
            fill="both",
            expand=True,
            padx=5
        )

        tk.Label(
            card,
            text=title,
            font=("Segoe UI", 9, "bold"),
            fg=MUTED,
            bg=PANEL
        ).pack(
            anchor="w",
            padx=18,
            pady=(16, 0)
        )

        label = tk.Label(
            card,
            text=value,
            font=("Segoe UI", 24, "bold"),
            fg=TEXT,
            bg=PANEL
        )

        label.pack(
            anchor="w",
            padx=18,
            pady=(3, 0)
        )

        tk.Label(
            card,
            text=subtitle,
            font=("Segoe UI", 8),
            fg=MUTED,
            bg=PANEL
        ).pack(
            anchor="w",
            padx=18,
            pady=(0, 15)
        )

        return label

    # ========================================================
    # SYNC PAGE
    # ========================================================

    def show_sync(self):

        self.clear_content()

        self.section_title(
            "Sincronizzazione",
            "Safe Sync tra questo PC e GitHub."
        )

        card = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        card.pack(
            fill="x",
            pady=15
        )

        self.info_row(
            card,
            "Repository",
            self.repo_path
        )

        self.info_row(
            card,
            "Remote",
            self.remote
        )

        self.info_row(
            card,
            "Branch",
            self.branch
        )

        self.info_row(
            card,
            "Auto Sync",
            "ATTIVO"
            if self.auto_sync_enabled
            else "DISATTIVO"
        )

        self.info_row(
            card,
            "Intervallo",
            f"{self.interval} secondi"
        )

        tk.Button(
            self.content,
            text="▶  SINCRONIZZA ORA",
            command=self.start_sync,
            bg=BLUE,
            fg="white",
            activebackground=BLUE,
            relief="flat",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            padx=22,
            pady=13
        ).pack(
            anchor="w",
            pady=10
        )

    # ========================================================
    # WORKSPACE PAGE
    # ========================================================

    def show_workspace(self):

        self.clear_content()

        tk.Label(self.content, text="VAULT SCUOLA — Workspace cifrato locale + sincronizzazione GitHub temporanea.", font=("Segoe UI", 9, "bold"), fg=BLUE, bg=BG).pack(anchor="w", pady=(4, 8))

        self.section_title(
            "Workspace",
            "Area di lavoro protetta di SchoolHub."
        )

        card = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        card.pack(
            fill="x",
            pady=15
        )

        is_unlocked = self.workspace.is_unlocked

        tk.Label(
            card,
            text="🔓" if is_unlocked else "🔒",
            font=("Segoe UI Emoji", 42),
            fg=GREEN if is_unlocked else TEXT,
            bg=PANEL
        ).pack(
            pady=(30, 5)
        )

        tk.Label(
            card,
            text="WORKSPACE SBLOCCATO"
            if is_unlocked
            else ("BLOCCO RAPIDO" if getattr(self.workspace, "has_plaintext", False) else "WORKSPACE BLOCCATO"),
            font=("Segoe UI", 16, "bold"),
            fg=GREEN if is_unlocked else TEXT,
            bg=PANEL
        ).pack()

        tk.Label(
            card,
            text=str(self.workspace_path),
            font=("Consolas", 9),
            fg=MUTED,
            bg=PANEL
        ).pack(
            pady=(5, 15)
        )

        tk.Label(card, text=f"Vault: {self.vault_path}", font=("Consolas", 8), fg=MUTED, bg=PANEL).pack(pady=(0, 15))

        # ----------------------------------------------------
        # NON ESISTE
        # ----------------------------------------------------

        if not self.workspace.exists:

            tk.Label(
                card,
                text=(
                    "Il Workspace non è ancora stato creato.\n"
                    "SchoolHub può configurarlo automaticamente."
                ),
                font=("Segoe UI", 9),
                fg=MUTED,
                bg=PANEL,
                justify="center"
            ).pack(
                pady=(0, 18)
            )

            tk.Button(
                card,
                text="🔐  CREA WORKSPACE",
                command=self.workspace_create,
                bg=GREEN,
                fg=BG,
                activebackground=GREEN,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                padx=24,
                pady=12
            ).pack(
                pady=(0, 30)
            )

            return

        # ----------------------------------------------------
        # SBLOCCATO
        # ----------------------------------------------------

        if is_unlocked:

            tk.Label(
                card,
                text=(
                    "I file sono disponibili in forma leggibile.\n"
                    "Blocco rapido = riapertura immediata; Cifra e chiudi = massima protezione."
                ),
                font=("Segoe UI", 9),
                fg=MUTED,
                bg=PANEL,
                justify="center"
            ).pack(
                pady=(0, 18)
            )

            buttons = tk.Frame(
                card,
                bg=PANEL
            )

            buttons.pack(
                pady=(0, 30)
            )

            tk.Button(
                buttons,
                text="📂  APRI WORKSPACE",
                command=self.open_workspace,
                bg=BLUE,
                fg="white",
                activebackground=BLUE,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                padx=20,
                pady=12
            ).pack(
                side="left",
                padx=5
            )

            tk.Button(
                buttons,
                text="⚡  BLOCCA RAPIDO",
                command=self.workspace_session_lock,
                bg=YELLOW,
                fg=BG,
                activebackground=YELLOW,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                padx=18,
                pady=12
            ).pack(
                side="left",
                padx=5
            )

            tk.Button(
                buttons,
                text="🔒  CIFRA E CHIUDI",
                command=self.workspace_lock,
                bg=RED,
                fg="white",
                activebackground=RED,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                padx=18,
                pady=12
            ).pack(
                side="left",
                padx=5
            )

        # ----------------------------------------------------
        # BLOCCATO
        # ----------------------------------------------------

        else:

            fast_locked = bool(getattr(self.workspace, "has_plaintext", False))
            tk.Label(
                card,
                text=(
                    "Blocco rapido attivo: i file sono già sul disco.\n"
                    "Inserisci la password: lo sblocco sarà quasi immediato."
                    if fast_locked else
                    "Il contenuto è cifrato.\n"
                    "Inserisci la password per renderlo disponibile."
                ),
                font=("Segoe UI", 9),
                fg=MUTED,
                bg=PANEL,
                justify="center"
            ).pack(
                pady=(0, 18)
            )

            buttons = tk.Frame(
                card,
                bg=PANEL
            )

            buttons.pack(
                pady=(0, 30)
            )

            tk.Button(
                buttons,
                text="🔓  SBLOCCA",
                command=self.workspace_unlock,
                bg=GREEN,
                fg=BG,
                activebackground=GREEN,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                padx=24,
                pady=12
            ).pack(
                side="left",
                padx=5
            )

    # ========================================================
    # CREATE WORKSPACE
    # ========================================================

    def workspace_create(self):
        if self.workspace_busy or self.sync_running:
            messagebox.showinfo("SchoolHub", "È già in corso un'operazione. Attendi che termini.", parent=self.root)
            return
        password = self.ask_password(
            "Crea Workspace protetto",
            "Scegli la password che proteggerà tutti i file del Vault. Non viene salvata da SchoolHub.",
            confirm=True,
            min_length=8,
        )
        if password is None:
            return
        self.workspace_busy = True
        self.open_progress("Creazione Workspace", "Cifratura e verifica del Vault")
        self.progress_update(2, "Preparazione", "Analisi dei file del Workspace")
        self.write_log("🔐 Creazione Workspace protetto.")
        def worker():
            try:
                self.workspace.create(password, progress=self.workspace_progress_callback(3, 97))
                self.progress_update(100, "Completato", "Vault creato e verificato")
                self.write_log("✓ Workspace protetto creato.")
                time.sleep(0.25)
                self.close_progress()
                if self.running:
                    self.root.after(0, lambda: messagebox.showinfo("SchoolHub", "Workspace creato correttamente.", parent=self.root))
                    self.root.after(100, lambda: self.navigate("Workspace"))
            except Exception as e:
                self.write_log(f"✕ Errore creazione Workspace: {e}")
                self.close_progress()
                if self.running:
                    self.root.after(0, lambda text=str(e): messagebox.showerror("Errore Workspace", text, parent=self.root))
            finally:
                self.workspace_busy = False
        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # UNLOCK
    # ========================================================

    def workspace_unlock(self):
        if self.workspace_busy or self.sync_running:
            messagebox.showinfo("SchoolHub", "È già in corso un'operazione sul Workspace. Attendi che termini.", parent=self.root)
            return
        password = self.ask_password("Sblocca Workspace", "Inserisci la password per decifrare il Vault in questa sessione.")
        if password is None:
            return
        self.workspace_busy = True
        self.open_progress("Sblocco Workspace", "Decifratura sicura dei file")
        self.progress_update(2, "Verifica password", "Controllo del Vault")
        self.write_log("🔓 Tentativo di sblocco Workspace.")
        def worker():
            try:
                self.workspace.unlock(password, progress=self.workspace_progress_callback(4, 97))
                self.progress_update(100, "Completato", "Workspace disponibile")
                self.write_log("✓ Workspace sbloccato.")
                warning = getattr(self.workspace, "last_warning", None)
                if warning: self.write_log("⚠ " + warning)
                time.sleep(0.2)
                self.close_progress()
                if self.running:
                    if warning:
                        self.root.after(0, lambda text=warning: messagebox.showwarning("Workspace - recupero", text, parent=self.root))
                    self.root.after(100, lambda: self.navigate("Workspace"))
            except Exception as e:
                error_text = str(e)
                self.write_log(f"✕ Sblocco Workspace fallito: {error_text}")
                self.close_progress()
                if self.running:
                    self.root.after(0, lambda text=error_text: messagebox.showerror("Workspace", text, parent=self.root))
            finally:
                self.workspace_busy = False
                if (
                    self.running
                    and self.auto_sync_enabled
                    and self.git_enabled
                    and self.remote
                    and self.workspace.is_unlocked
                ):
                    self.root.after(300, self._auto_sync_after_unlock)
        threading.Thread(target=worker, daemon=True).start()

    def workspace_session_lock(self):
        if self.workspace_busy or self.sync_running:
            messagebox.showinfo("SchoolHub", "È già in corso un'operazione sul Workspace. Attendi che termini.", parent=self.root)
            return
        if not self.workspace.is_unlocked:
            return
        if not messagebox.askyesno(
            "Blocco rapido",
            "SchoolHub dimenticherà la chiave della sessione, ma i file resteranno leggibili sul disco.\n\n"
            "Il prossimo sblocco sarà quasi immediato dopo la verifica password.\n"
            "Per protezione completa usa 'Cifra e chiudi'.\n\nContinuare?",
            parent=self.root,
        ):
            return
        self.workspace.session_lock()
        self.write_log("⚡ Blocco rapido: chiave rimossa dalla memoria, file locali mantenuti.")
        self.navigate("Workspace")

    def workspace_lock(self):
        if self.workspace_busy or self.sync_running:
            messagebox.showinfo("SchoolHub", "È già in corso un'operazione sul Workspace. Attendi che termini.", parent=self.root)
            return
        password = self.ask_password("Blocca Workspace", "Conferma la password per cifrare tutto e rimuovere la copia leggibile.")
        if password is None:
            return
        if not messagebox.askyesno("Blocca Workspace", "Il Workspace verrà cifrato e la copia leggibile verrà rimossa solo dopo la verifica completa.\n\nContinuare?", parent=self.root):
            return
        self.workspace_busy = True
        self.open_progress("Blocco Workspace", "Cifratura, verifica e chiusura sicura")
        self.progress_update(2, "Preparazione", "Controllo dei file aperti")
        self.write_log("🔒 Avvio Cifra e chiudi.")
        def worker():
            try:
                self.workspace.lock(password, progress=self.workspace_progress_callback(3, 98))
                self.progress_update(100, "Completato", "Workspace cifrato e bloccato")
                self.write_log("✓ Workspace bloccato e cifrato.")
                time.sleep(0.2)
                self.close_progress()
                if self.running: self.root.after(0, lambda: self.navigate("Workspace"))
            except Exception as e:
                self.write_log(f"✕ Blocco Workspace fallito: {e}")
                self.close_progress()
                if self.running:
                    self.root.after(0, lambda text=str(e): messagebox.showerror("Workspace", text, parent=self.root))
                    self.root.after(100, lambda: self.navigate("Workspace"))
            finally:
                self.workspace_busy = False
        threading.Thread(target=worker, daemon=True).start()

    def open_workspace(self):
        if self.workspace_busy or self.sync_running:
            messagebox.showinfo("SchoolHub", "Il Workspace è occupato da un'operazione. Attendi che termini.")
            return
        if not self.workspace.is_unlocked:
            messagebox.showwarning("Workspace", "Il Workspace è bloccato. Sbloccalo prima di aprirlo.")
            return
        try:
            self.workspace.ensure_materialized()
            path = self.workspace.get_unlocked_path()
        except Exception as exc:
            messagebox.showerror("Workspace", f"Impossibile materializzare il Workspace:\n{exc}")
            return
        if not os.path.isdir(path):
            messagebox.showerror("Workspace", "Cartella Workspace non disponibile.")
            return
        try:
            os.startfile(str(path))
        except Exception as e:
            messagebox.showerror("Workspace", f"Impossibile aprire il Workspace:\n{e}")

    def _resolve_conflict(self, choice):
        """Resolve all currently conflicting files in one explicit operation.
        choice='local' keeps the encrypted Workspace version.
        choice='remote' keeps the GitHub version.
        """
        if not self.git_enabled:
            return

        if self.sync_running:
            messagebox.showinfo("SchoolHub", "Una sincronizzazione è già in corso.")
            return
        if self.workspace_busy:
            messagebox.showinfo("SchoolHub", "È in corso un'operazione sul Workspace. Attendi che termini.")
            return

        password = None
        if not self.workspace.exists:
            messagebox.showwarning("Workspace", "Workspace cifrato non trovato.")
            return

        if not self.workspace.is_unlocked:
            password = self.ask_password("Password Workspace", "Inserisci la password del Workspace per risolvere i conflitti.")
            if password is None:
                return

        if choice == "local":
            question = (
                "Vuoi usare il Workspace come versione definitiva?\n\n"
                "I file in conflitto su GitHub verranno sostituiti "
                "dalla copia locale e verrà creato un nuovo commit."
            )
        else:
            question = (
                "Vuoi usare GitHub come versione definitiva?\n\n"
                "I file in conflitto locali verranno sostituiti "
                "dalla copia presente su GitHub."
            )

        if not messagebox.askyesno("Risolvi conflitti", question, parent=self.root):
            return

        self.sync_running = True
        self.set_status("RISOLUZIONE CONFLITTI...", YELLOW, "Operazione esplicita richiesta")
        threading.Thread(
            target=self._resolve_conflict_worker,
            args=(choice, password),
            daemon=True
        ).start()

    def _resolve_conflict_worker(self, choice, password):
        temp_root = tempfile.mkdtemp(prefix="schoolhub-conflict-")
        repo = os.path.join(temp_root, "repo")
        try:
            source, _ = self._get_sync_source(password, temp_root)
            self._assert_remote_private()
            self.write_log("↔ Scaricamento GitHub per risolvere i conflitti...")
            self._clone_remote(repo)

            state = self._load_sync_state()
            baseline = state.get("files", {}) if state is not None else {}
            local_files = self._hash_tree(source)
            remote_files = self._hash_tree(repo)
            local_only, remote_only, conflicts = self._classify_changes(baseline, local_files, remote_files)
            if not conflicts:
                self.last_conflicts = []
                raise WorkspaceError("Non risultano più conflitti. Aggiorna lo stato e sincronizza normalmente.")

            # Start from GitHub. Preserve independent local changes, and apply the
            # conflicting local paths only when the user explicitly chose Workspace.
            paths_to_take_local = set(local_only)
            if choice == "local":
                paths_to_take_local.update(conflicts)

            if self.workspace.is_unlocked and self._hash_tree(self.workspace.get_unlocked_path()) != local_files:
                raise WorkspaceError(
                    "Il Workspace è stato modificato durante la risoluzione. Nessun file viene sovrascritto: aggiorna i conflitti e riprova."
                )

            if paths_to_take_local:
                self._apply_paths(source, repo, local_files, paths_to_take_local)

            paths_from_remote = set(remote_only)
            if choice == "remote":
                paths_from_remote.update(conflicts)
            self._persist_sync_tree(repo, password, temp_root, paths_from_remote)

            self._ensure_git_identity(repo)
            code, _, err = self.git(["add", "-A"], cwd=repo)
            if code != 0:
                raise WorkspaceError(err or "git add fallito.")
            code, out, err = self.git(["commit", "-m", "SchoolHub: risoluzione conflitti"], cwd=repo)
            if code != 0 and "nothing to commit" not in (out + " " + err).lower():
                raise WorkspaceError(err or out or "git commit fallito.")
            self._push_with_auth_retry(repo)
            self._verify_remote_head(repo)

            final_files = self._hash_tree(repo)
            self._save_sync_state(final_files)
            self.last_conflicts = []
            if choice == "local":
                self.write_log(f"✓ Conflitti risolti: mantenuto Workspace per {len(conflicts)} file.")
                self.finish_status("SINCRONIZZATO", GREEN, "Conflitti risolti: mantenuto Workspace")
            else:
                self.write_log(f"✓ Conflitti risolti: mantenuto GitHub per {len(conflicts)} file.")
                self.finish_status("SINCRONIZZATO", GREEN, "Conflitti risolti: mantenuto GitHub")
        except Exception as e:
            self.write_log(f"✕ Risoluzione conflitti fallita: {e}")
            self.finish_status("ERRORE", RED, str(e))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            self.sync_running = False
            if self.running:
                self.root.after(0, self.refresh_status)

    def show_conflicts(self):
        self.clear_content()

        self.section_title(
            "Conflitti",
            "Controllo reale tra Workspace e GitHub. Nessun file viene sovrascritto automaticamente."
        )

        card = tk.Frame(
            self.content, bg=PANEL,
            highlightbackground=BORDER, highlightthickness=1
        )
        card.pack(fill="x", pady=15)

        status_label = tk.Label(
            card, text="CONTROLLO IN CORSO...",
            font=("Segoe UI", 15, "bold"), fg=YELLOW, bg=PANEL
        )
        status_label.pack(anchor="w", padx=22, pady=(22, 5))

        detail_label = tk.Label(
            card, text="Confronto Workspace e GitHub con la baseline dell'ultima sync.",
            font=("Segoe UI", 9), fg=MUTED, bg=PANEL,
            justify="left", wraplength=800
        )
        detail_label.pack(anchor="w", padx=22, pady=(0, 10))

        help_label = tk.Label(
            card,
            text="Se entrambi sono cambiati, puoi scegliere esplicitamente quale versione mantenere.",
            font=("Segoe UI", 9), fg=MUTED, bg=PANEL,
            justify="left", wraplength=800
        )
        help_label.pack(anchor="w", padx=22, pady=(0, 10))

        conflict_list = tk.Listbox(
            card, height=7, bg=PANEL2, fg=TEXT, selectbackground=BLUE,
            selectforeground="white", relief="flat", bd=0, font=("Consolas", 9)
        )
        conflict_list.pack(fill="x", padx=22, pady=(0, 22))

        buttons = tk.Frame(self.content, bg=BG)
        buttons.pack(anchor="w", pady=10)

        tk.Button(
            buttons, text="MANTIENI WORKSPACE",
            command=lambda: self._resolve_conflict("local"),
            bg=BLUE, fg="white", activebackground=BLUE,
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 10, "bold"), padx=20, pady=11
        ).pack(side="left")

        tk.Button(
            buttons, text="MANTIENI GITHUB",
            command=lambda: self._resolve_conflict("remote"),
            bg=PANEL2, fg=TEXT, activebackground=BORDER,
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 10, "bold"), padx=20, pady=11
        ).pack(side="left", padx=10)

        tk.Button(
            buttons, text="SINCRONIZZA",
            command=self.start_sync,
            bg=PANEL2, fg=TEXT, activebackground=BORDER,
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 10, "bold"), padx=20, pady=11
        ).pack(side="left")

        tk.Button(
            buttons, text="AGGIORNA",
            command=self.show_conflicts,
            bg=PANEL2, fg=TEXT, activebackground=BORDER,
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 10, "bold"), padx=20, pady=11
        ).pack(side="left", padx=10)

        generation = self.page_generation

        def worker():
            local, remote, changes = self.get_status()

            def update():
                if not self.running or generation != self.page_generation:
                    return
                try:
                    conflict_list.delete(0, tk.END)
                    if local is None:
                        status_label.configure(text="IMPOSSIBILE CONTROLLARE", fg=RED)
                        detail_label.configure(
                            text=str(remote or "Repository/GitHub non disponibile.")
                        )
                        return

                    if local > 0 and remote > 0:
                        status_label.configure(text="⚠ CONFLITTO RILEVATO", fg=RED)
                        detail_label.configure(
                            text=(
                                f"Workspace: {local} file cambiati · GitHub: {remote} file cambiati.\n"
                                f"I file sotto sono quelli che sono stati modificati da entrambe le parti.\n"
                                "Solo questi sono veri conflitti. Scegli quale versione mantenere per TUTTI i file elencati."
                            )
                        )
                        for path in self.last_conflicts:
                            conflict_list.insert(tk.END, path)
                        conflict_list.configure(height=min(12, max(3, len(self.last_conflicts))))
                    elif local > 0:
                        status_label.configure(text="MODIFICHE SOLO LOCALI", fg=YELLOW)
                        detail_label.configure(
                            text=f"{local} file modificati nel Workspace. Nessun conflitto: verranno sincronizzati automaticamente."
                        )
                    elif remote > 0:
                        status_label.configure(text="MODIFICHE SOLO SU GITHUB", fg=YELLOW)
                        detail_label.configure(
                            text=f"{remote} file modificati su GitHub. Nessun conflitto: verranno importati automaticamente."
                        )
                    else:
                        status_label.configure(text="✓ NESSUN CONFLITTO", fg=GREEN)
                        detail_label.configure(
                            text="Workspace e GitHub sono sincronizzati."
                        )
                except tk.TclError:
                    pass

            if self.running:
                self.root.after(0, update)

        if self.git_enabled:
            threading.Thread(target=worker, daemon=True).start()
        else:
            status_label.configure(text="VAULT LOCALE", fg=GREEN)
            detail_label.configure(
                text="Questo Vault non usa GitHub. I conflitti Git non si applicano."
            )
            help_label.configure(text="Il Vault è completamente locale e indipendente.")

    # ========================================================
    # ACTIVITY
    # ========================================================

    def show_activity(self):

        self.clear_content()

        self.section_title(
            "Attività",
            "Registro delle operazioni di SchoolHub."
        )

        card = tk.Frame(
            self.content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        card.pack(
            fill="both",
            expand=True,
            pady=15
        )

        self.activity_box = tk.Text(
            card,
            bg=PANEL,
            fg=MUTED,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            font=("Consolas", 9),
            wrap="word"
        )

        self.activity_box.pack(
            fill="both",
            expand=True,
            padx=22,
            pady=20
        )

        self.activity_box.configure(
            state="disabled"
        )

        buttons = tk.Frame(
            self.content,
            bg=BG
        )

        buttons.pack(
            anchor="w",
            pady=5
        )

        tk.Button(
            buttons,
            text="AGGIORNA LOG",
            command=self.load_recent_log,
            bg=PANEL2,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
            padx=18,
            pady=10
        ).pack(
            side="left"
        )

        self.load_recent_log()

    # ========================================================
    # GUIDED GITHUB SETUP
    # ========================================================

    def show_github_setup(self, first_run=False):
        if self.sync_running or self.workspace_busy:
            messagebox.showinfo("SchoolHub", "Termina prima l'operazione in corso.", parent=self.root)
            return

        win = tk.Toplevel(self.root)
        win.title("Configura SchoolHub")
        win.configure(bg=BG)
        win.geometry("760x610")
        win.minsize(700, 560)
        win.transient(self.root)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        state = {"repos": [], "display_map": {}, "login": self.github_user or ""}
        shell = tk.Frame(win, bg=BG)
        shell.pack(fill="both", expand=True, padx=32, pady=26)

        tk.Label(
            shell,
            text="CONFIGURAZIONE GUIDATA",
            font=("Segoe UI", 22, "bold"),
            fg=TEXT,
            bg=BG,
        ).pack(anchor="w")
        tk.Label(
            shell,
            text="Accedi a GitHub, scegli il repository e poi il branch. Nessun token viene salvato da SchoolHub.",
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=BG,
            wraplength=680,
            justify="left",
        ).pack(anchor="w", pady=(4, 18))

        card = tk.Frame(shell, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)

        account_status = tk.Label(
            card,
            text="1. Accedi con il tuo account GitHub",
            font=("Segoe UI", 11, "bold"),
            fg=TEXT,
            bg=PANEL,
        )
        account_status.pack(anchor="w", padx=22, pady=(20, 8))

        login_button = tk.Button(
            card,
            text="ACCEDI A GITHUB",
            bg=BLUE,
            fg="white",
            activebackground=BLUE,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            padx=20,
            pady=10,
        )
        login_button.pack(anchor="w", padx=22)

        tk.Label(card, text="2. Repository", font=("Segoe UI",9,"bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=22, pady=(20,5))
        repo_var = tk.StringVar()
        repo_box = ttk.Combobox(card, textvariable=repo_var, state="disabled", font=("Segoe UI", 10))
        repo_box.pack(fill="x", padx=22, ipady=5)

        tk.Label(card, text="3. Branch", font=("Segoe UI",9,"bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=22, pady=(16,5))
        branch_var = tk.StringVar()
        branch_box = ttk.Combobox(card, textvariable=branch_var, state="disabled", font=("Segoe UI", 10))
        branch_box.pack(fill="x", padx=22, ipady=5)

        options = tk.Frame(card, bg=PANEL)
        options.pack(fill="x", padx=22, pady=(18, 4))
        auto_var = tk.BooleanVar(value=self.auto_sync_enabled)
        start_var = tk.BooleanVar(value=self.auto_start_enabled)
        tk.Checkbutton(options, text="Sincronizzazione automatica", variable=auto_var, bg=PANEL, fg=TEXT, selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT).pack(anchor="w")
        tk.Checkbutton(options, text="Avvia SchoolHub con Windows", variable=start_var, bg=PANEL, fg=TEXT, selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT).pack(anchor="w", pady=(5,0))

        tk.Label(card, text="Nome di questo dispositivo", font=("Segoe UI",9,"bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=22, pady=(14,5))
        device_var = tk.StringVar(value=self.device_name)
        device_entry = tk.Entry(card, textvariable=device_var, bg=PANEL2, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI",10))
        device_entry.pack(fill="x", padx=22, ipady=7)

        status = tk.Label(card, text="", font=("Segoe UI",8,"bold"), fg=YELLOW, bg=PANEL, wraplength=660, justify="left")
        status.pack(anchor="w", padx=22, pady=(12,6))

        footer = tk.Frame(card, bg=PANEL)
        footer.pack(fill="x", padx=22, pady=(8,20))

        save_button = tk.Button(
            footer,
            text="SALVA E CONTINUA",
            state="disabled",
            bg=GREEN,
            fg="#06120b",
            activebackground=GREEN,
            activeforeground="#06120b",
            disabledforeground=MUTED,
            relief="flat",
            bd=0,
            font=("Segoe UI",10,"bold"),
            cursor="hand2",
            padx=22,
            pady=10,
        )
        save_button.pack(side="right")

        def set_busy(text_value):
            status.configure(text=text_value, fg=YELLOW)
            login_button.configure(state="disabled")
            repo_box.configure(state="disabled")
            branch_box.configure(state="disabled")
            save_button.configure(state="disabled")

        def finish_login(login, repos):
            if not win.winfo_exists():
                return
            state["login"] = login
            state["repos"] = repos
            state["display_map"] = {}
            displays = []
            for item in repos:
                label = item["full_name"] + ("  [privato]" if item["private"] else "  [pubblico]")
                state["display_map"][label] = item
                displays.append(label)
            account_status.configure(text=f"✓ GitHub: {login or 'account collegato'}", fg=GREEN)
            repo_box.configure(values=displays, state="readonly")
            login_button.configure(text="CAMBIA ACCOUNT", state="normal")
            status.configure(text=f"{len(displays)} repository disponibili. Scegline uno.", fg=GREEN)
            if displays:
                preferred = None
                for label, item in state["display_map"].items():
                    if item["clone_url"].rstrip("/") == (self.remote or "").rstrip("/"):
                        preferred = label
                        break
                repo_var.set(preferred or displays[0])
                load_branches()
            else:
                status.configure(text="Nessun repository disponibile per questo account.", fg=RED)

        def login_worker():
            try:
                login, repos = self._github_account_and_repositories()
                self.root.after(0, lambda: finish_login(login, repos))
            except Exception as exc:
                self.root.after(0, lambda text=str(exc): (
                    status.configure(text=text, fg=RED),
                    login_button.configure(state="normal"),
                ))

        def do_login():
            set_busy("Apro GitHub nel browser. Completa l'accesso...")
            threading.Thread(target=login_worker, daemon=True).start()

        def finish_branches(branches, default_branch):
            if not win.winfo_exists():
                return
            values = branches or [default_branch or "main"]
            branch_box.configure(values=values, state="readonly")
            branch_var.set(default_branch if default_branch in values else values[0])
            repo_box.configure(state="readonly")
            login_button.configure(state="normal")
            save_button.configure(state="normal")
            status.configure(text="Repository e branch pronti. Premi SALVA E CONTINUA.", fg=GREEN)

        def branches_worker(full_name, default_branch):
            try:
                branches = self._github_repository_branches(full_name)
                self.root.after(0, lambda: finish_branches(branches, default_branch))
            except Exception as exc:
                self.root.after(0, lambda text=str(exc): (
                    status.configure(text=text, fg=RED),
                    repo_box.configure(state="readonly"),
                    login_button.configure(state="normal"),
                ))

        def load_branches(_event=None):
            item = state["display_map"].get(repo_var.get())
            if not item:
                return
            set_busy("Carico i branch del repository...")
            repo_box.configure(state="disabled")
            threading.Thread(
                target=branches_worker,
                args=(item["full_name"], item["default_branch"]),
                daemon=True,
            ).start()

        def save_guided():
            item = state["display_map"].get(repo_var.get())
            branch_name = branch_var.get().strip()
            if not item or not branch_name:
                status.configure(text="Seleziona repository e branch.", fg=RED)
                return
            name = device_var.get().strip() or ("Dispositivo " + self.device_id[:6].upper())
            candidate = dict(self.config)
            candidate.update({
                "remote": item["clone_url"],
                "branch": branch_name,
                "github_user": state["login"],
                "auto_sync": bool(auto_var.get()),
                "auto_start": bool(start_var.get()),
                "onboarding_complete": True,
                "device_id": self.device_id,
                "device_name": name[:80],
            })
            try:
                save_config(candidate)
                verified = load_config()
                for key in ("remote", "branch", "github_user", "device_id", "device_name"):
                    if verified.get(key) != candidate.get(key):
                        raise WorkspaceError(f"Verifica salvataggio fallita: {key}")
                self.config = verified
                self.remote = verified["remote"]
                self.branch = verified["branch"]
                self.github_user = verified.get("github_user", "")
                self.git_enabled = bool(self.remote)
                self.auto_sync_enabled = bool(verified.get("auto_sync", False))
                self.auto_start_enabled = bool(verified.get("auto_start", False))
                self.onboarding_complete = True
                self.device_name = verified.get("device_name", name)
                startup_ok = self.set_windows_startup(self.auto_start_enabled)
                if self.auto_start_enabled and not startup_ok:
                    self.auto_start_enabled = False
                    self.config["auto_start"] = False
                    save_config(self.config)
                self.schedule_auto_sync()
                win.destroy()
                self.navigate("Home")
                messagebox.showinfo(
                    "SchoolHub",
                    f"GitHub configurato correttamente.\n\n{item['full_name']}\nBranch: {branch_name}",
                    parent=self.root,
                )
            except Exception as exc:
                status.configure(text=f"Salvataggio fallito: {exc}", fg=RED)

        def local_only():
            self.config["onboarding_complete"] = True
            self.config["device_name"] = device_var.get().strip() or self.device_name
            save_config(self.config)
            self.onboarding_complete = True
            self.device_name = self.config["device_name"]
            win.destroy()

        login_button.configure(command=do_login)
        repo_box.bind("<<ComboboxSelected>>", load_branches)
        save_button.configure(command=save_guided)

        if first_run:
            tk.Button(
                footer,
                text="SOLO LOCALE PER ORA",
                command=local_only,
                bg=PANEL3,
                fg=TEXT,
                activebackground=BORDER,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                cursor="hand2",
                padx=16,
                pady=10,
            ).pack(side="left")
        else:
            tk.Button(
                footer,
                text="ANNULLA",
                command=win.destroy,
                bg=PANEL3,
                fg=TEXT,
                activebackground=BORDER,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                cursor="hand2",
                padx=16,
                pady=10,
            ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", local_only if first_run else win.destroy)
        if self.github_user and self.remote:
            account_status.configure(text=f"GitHub configurato: {self.github_user}", fg=GREEN)

    # ========================================================
    # SETTINGS
    # ========================================================

    def show_settings(self):
        self.clear_content()
        self.section_title("Impostazioni", "Sicurezza, sincronizzazione, avvio e aggiornamenti di SchoolHub.")

        top = tk.Frame(self.content, bg=BG)
        top.pack(fill="x", pady=(14, 10))

        version_card = tk.Frame(top, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        version_card.pack(side="left", fill="both", expand=True, padx=(0,8))
        tk.Label(version_card, text="SCHOOLHUB", font=("Segoe UI",9,"bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=20, pady=(17,3))
        tk.Label(version_card, text=f"Versione {APP_VERSION}", font=("Segoe UI",18,"bold"), fg=TEXT, bg=PANEL).pack(anchor="w", padx=20)
        tk.Label(version_card, text="Aggiornamenti firmati tramite checksum SHA-256", font=("Segoe UI",8), fg=MUTED, bg=PANEL).pack(anchor="w", padx=20, pady=(4,16))

        update_card = tk.Frame(top, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        update_card.pack(side="left", fill="both", expand=True, padx=(8,0))
        tk.Label(update_card, text="AGGIORNAMENTI", font=("Segoe UI",9,"bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=20, pady=(17,4))
        tk.Label(update_card, text="Controllo automatico all'avvio", font=("Segoe UI",10,"bold"), fg=GREEN, bg=PANEL).pack(anchor="w", padx=20)
        tk.Button(update_card, text="CONTROLLA AGGIORNAMENTI", command=lambda: self.check_for_updates(silent=False), bg=PANEL3, fg=TEXT, activebackground=BLUE, activeforeground="white", relief="flat", bd=0, font=("Segoe UI",9,"bold"), cursor="hand2", padx=14, pady=9).pack(anchor="w", padx=20, pady=(10,15))

        card = tk.Frame(self.content, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(8, 10))

        tk.Label(card, text="WORKSPACE SICURO", font=("Segoe UI", 9, "bold"), fg=CYAN, bg=PANEL).pack(anchor="w", padx=22, pady=(18, 7))
        tk.Label(card, text=self.workspace_path, font=("Consolas", 9), fg=TEXT, bg=PANEL).pack(anchor="w", padx=22, pady=(0, 8))
        tk.Label(card, text="Vault: " + self.vault_path, font=("Consolas", 8), fg=MUTED, bg=PANEL).pack(anchor="w", padx=22, pady=(0, 18))

        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", padx=22, pady=(0,16))

        github_head = tk.Frame(card, bg=PANEL)
        github_head.pack(fill="x", padx=22, pady=(0,8))
        github_head_left = tk.Frame(github_head, bg=PANEL)
        github_head_left.pack(side="left", fill="x", expand=True)
        tk.Label(github_head_left, text="SINCRONIZZAZIONE GITHUB", font=("Segoe UI", 9, "bold"), fg=CYAN, bg=PANEL).pack(anchor="w")
        self.settings_dirty_label = tk.Label(
            github_head_left,
            text="Tutto salvato",
            font=("Segoe UI", 8, "bold"),
            fg=GREEN,
            bg=PANEL,
        )
        self.settings_dirty_label.pack(anchor="w", pady=(3,0))
        tk.Button(
            github_head,
            text="CONFIGURAZIONE GUIDATA",
            command=lambda: self.show_github_setup(first_run=False),
            bg=PANEL3,
            fg=TEXT,
            activebackground=BLUE,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            padx=16,
            pady=10,
        ).pack(side="right", padx=(8,0))

        tk.Button(
            github_head,
            text="✓  SALVA",
            command=self.save_settings,
            bg=GREEN,
            fg="#06120b",
            activebackground=GREEN,
            activeforeground="#06120b",
            relief="flat",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            padx=26,
            pady=10,
        ).pack(side="right", padx=(16,0))

        tk.Label(card, text="Repository GitHub pubblico o privato: SchoolHub sincronizza usando la modalità standard configurata.", font=("Segoe UI",8), fg=MUTED, bg=PANEL, wraplength=760, justify="left").pack(anchor="w", padx=22, pady=(0,10))

        tk.Label(card, text="URL REPOSITORY GITHUB", font=("Segoe UI", 8, "bold"), fg=MUTED, bg=PANEL).pack(anchor="w", padx=22, pady=(0,5))
        self.remote_entry = tk.Entry(card, bg=PANEL2, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 9))
        self.remote_entry.pack(fill="x", padx=22, pady=(0, 13), ipady=9)
        self.remote_entry.insert(0, self.remote)

        row = tk.Frame(card,bg=PANEL); row.pack(fill="x", padx=22, pady=(0,15))
        left=tk.Frame(row,bg=PANEL); left.pack(side="left")
        tk.Label(left, text="BRANCH", font=("Segoe UI",8,"bold"), fg=MUTED, bg=PANEL).pack(anchor="w")
        self.branch_entry=tk.Entry(left,width=18,bg=PANEL2,fg=TEXT,insertbackground=TEXT,relief="flat",font=("Segoe UI",9)); self.branch_entry.pack(anchor="w",pady=(5,0),ipady=8); self.branch_entry.insert(0,self.branch)
        right=tk.Frame(row,bg=PANEL); right.pack(side="left",padx=(28,0))
        tk.Label(right,text="INTERVALLO SYNC (MINUTI)",font=("Segoe UI",8,"bold"),fg=MUTED,bg=PANEL).pack(anchor="w")
        self.interval_entry=tk.Entry(right,width=10,bg=PANEL2,fg=TEXT,insertbackground=TEXT,relief="flat",font=("Segoe UI",9)); self.interval_entry.pack(anchor="w",pady=(5,0),ipady=8); self.interval_entry.insert(0,str(max(1,self.interval//60)))

        self.auto_var = tk.BooleanVar(value=self.auto_sync_enabled)
        tk.Checkbutton(card, text="Sincronizzazione automatica", variable=self.auto_var, bg=PANEL, fg=TEXT, selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT, font=("Segoe UI", 9)).pack(anchor="w", padx=22, pady=(0, 8))
        self.start_var = tk.BooleanVar(value=self.auto_start_enabled)
        tk.Checkbutton(card, text="Avvia SchoolHub automaticamente con Windows", variable=self.start_var, bg=PANEL, fg=TEXT, selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT, font=("Segoe UI", 9)).pack(anchor="w", padx=22, pady=(0, 14))

        tk.Label(
            card,
            text="Premi SALVA in alto (oppure Ctrl+S) per rendere permanenti URL, branch, intervallo e spunte.",
            font=("Segoe UI", 8),
            fg=MUTED,
            bg=PANEL,
            wraplength=760,
            justify="left",
        ).pack(anchor="w", padx=22, pady=(2, 18))

        def mark_settings_dirty(*_):
            try:
                self.settings_dirty_label.configure(text="MODIFICHE NON SALVATE", fg=YELLOW)
            except Exception:
                pass

        self.remote_entry.bind("<KeyRelease>", mark_settings_dirty)
        self.branch_entry.bind("<KeyRelease>", mark_settings_dirty)
        self.interval_entry.bind("<KeyRelease>", mark_settings_dirty)
        self.auto_var.trace_add("write", mark_settings_dirty)
        self.start_var.trace_add("write", mark_settings_dirty)
        self.root.bind("<Control-s>", lambda _event: self.save_settings())
        self.root.bind("<Control-S>", lambda _event: self.save_settings())

    def _show_settings_saved_popup(self, verified):
        """Show an unmistakable modal confirmation after disk read-back succeeds."""
        win = tk.Toplevel(self.root)
        win.title("SchoolHub")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.grab_set()

        width, height = 500, 300
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")

        shell = tk.Frame(win, bg=BG)
        shell.pack(fill="both", expand=True, padx=28, pady=26)
        tk.Label(shell, text="✓", font=("Segoe UI Symbol", 38, "bold"), fg=GREEN, bg=BG).pack()
        tk.Label(
            shell,
            text="SALVATO CORRETTAMENTE",
            font=("Segoe UI", 17, "bold"),
            fg=TEXT,
            bg=BG,
        ).pack(pady=(7, 6))
        tk.Label(
            shell,
            text=(
                "Le impostazioni sono state scritte su disco e rilette con successo.\n\n"
                f"Repository: {verified.get('remote') or 'nessuno'}\n"
                f"Branch: {verified.get('branch') or DEFAULT_BRANCH}\n"
                f"Sync automatica: {'ATTIVA' if verified.get('auto_sync') else 'DISATTIVA'}\n"
                f"Avvio Windows: {'ATTIVO' if verified.get('auto_start') else 'DISATTIVO'}"
            ),
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=BG,
            justify="center",
        ).pack(pady=(0, 18))
        tk.Button(
            shell,
            text="OK",
            command=win.destroy,
            bg=BLUE,
            fg="white",
            activebackground=BLUE,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            padx=32,
            pady=10,
        ).pack()
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.focus_force()

    def save_settings(self):
        if self.sync_running or self.workspace_busy:
            messagebox.showinfo(
                "SchoolHub",
                "Attendi il termine dell'operazione in corso prima di cambiare le impostazioni.",
                parent=self.root,
            )
            return

        new_remote = self.remote_entry.get().strip()
        new_branch = self.branch_entry.get().strip() or DEFAULT_BRANCH
        if new_remote and not (
            new_remote.startswith("https://github.com/")
            or new_remote.startswith("http://github.com/")
        ):
            messagebox.showerror(
                "Repository non valido",
                "Inserisci un URL GitHub HTTPS, oppure lascia il campo vuoto per usare SchoolHub solo in locale.",
                parent=self.root,
            )
            return

        try:
            interval_minutes = int(self.interval_entry.get().strip())
            if interval_minutes < 1 or interval_minutes > 1440:
                raise ValueError
        except Exception:
            messagebox.showerror(
                "Intervallo non valido",
                "L'intervallo deve essere un numero tra 1 e 1440 minuti.",
                parent=self.root,
            )
            return

        import re
        m = re.match(r"https?://github\.com/([^/]+)/", new_remote)
        new_github_user = m.group(1) if m else ""
        requested_auto_sync = bool(self.auto_var.get()) and bool(new_remote)
        requested_auto_start = bool(self.start_var.get())
        new_interval = interval_minutes * 60

        # First persist the exact UI state.
        candidate = dict(self.config)
        candidate.update({
            "version": 7,
            "remote": new_remote,
            "branch": new_branch,
            "github_user": new_github_user,
            "auto_sync": requested_auto_sync,
            "auto_start": requested_auto_start,
            "interval": new_interval,
            "workspace_path": self.workspace_path,
            "vault_path": self.vault_path,
            "sync_state_file": self.sync_state_file,
        })
        try:
            save_config(candidate)
        except Exception as exc:
            self.write_log(f"✕ Salvataggio impostazioni fallito: {exc}")
            messagebox.showerror(
                "Salvataggio fallito",
                f"Non riesco a scrivere config.json.\n\n{exc}",
                parent=self.root,
            )
            return

        # Apply Windows startup, then persist the actual verified state.
        startup_ok = self.set_windows_startup(requested_auto_start)
        actual_auto_start = requested_auto_start and startup_ok
        if requested_auto_start and not startup_ok:
            self.start_var.set(False)
            candidate["auto_start"] = False
            try:
                save_config(candidate)
            except Exception as exc:
                messagebox.showerror(
                    "Salvataggio fallito",
                    f"Impossibile salvare lo stato finale dell'avvio Windows.\n\n{exc}",
                    parent=self.root,
                )
                return

        # Read the file back and require exact persistence before claiming success.
        try:
            verified = load_config()
        except Exception as exc:
            messagebox.showerror(
                "Verifica salvataggio fallita",
                f"Le impostazioni sono state scritte ma non possono essere rilette.\n\n{exc}",
                parent=self.root,
            )
            return

        expected = {
            "remote": new_remote,
            "branch": new_branch,
            "github_user": new_github_user,
            "auto_sync": requested_auto_sync,
            "auto_start": actual_auto_start,
            "interval": new_interval,
        }
        mismatches = {
            key: (expected[key], verified.get(key))
            for key in expected
            if verified.get(key) != expected[key]
        }
        if mismatches:
            self.write_log(f"✕ Verifica impostazioni fallita: {mismatches}")
            messagebox.showerror(
                "Verifica salvataggio fallita",
                "SchoolHub ha rilevato che alcuni valori non sono rimasti salvati su disco.\n"
                "Nessun messaggio di successo è stato mostrato. Controlla Attività.",
                parent=self.root,
            )
            return

        # Only now update the live runtime state.
        self.config = verified
        self.remote = verified["remote"]
        self.branch = verified["branch"]
        self.github_user = verified.get("github_user", "")
        self.interval = int(verified["interval"])
        self.auto_sync_enabled = bool(verified["auto_sync"])
        self.auto_start_enabled = bool(verified["auto_start"])
        self.git_enabled = bool(self.remote)
        self.schedule_auto_sync()

        self.write_log(
            "✓ Impostazioni GitHub salvate, rilette e verificate su disco: "
            f"remote={self.remote or '-'} branch={self.branch} "
            f"auto_sync={self.auto_sync_enabled} auto_start={self.auto_start_enabled} "
            f"interval={self.interval}s"
        )
        try:
            self.settings_dirty_label.configure(text="TUTTO SALVATO", fg=GREEN)
        except Exception:
            pass
        self._show_settings_saved_popup(verified)

    def clone_repository(self):
        messagebox.showinfo("SchoolHub", "SchoolHub non mantiene repository Git locali permanenti. Usa Git solo nel Vault Git configurato e in una cartella temporanea durante la sincronizzazione.")

    # ========================================================
    # GIT / ENCRYPTED WORKSPACE SYNC
    # ========================================================

    def git(self, args, cwd=None, timeout=GIT_TIMEOUT_SECONDS, input_text=None):
        """Run Git hidden with a hard timeout so SchoolHub cannot sync forever."""
        if not cwd:
            return 1, "", "Repository temporaneo non disponibile."

        def run_git(git_args, input_text=None):
            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                git_exe, git_env, _ = bundled_git_info()
                if not git_exe:
                    return 127, "", "Git integrato non disponibile e Git di sistema non trovato."
                cp = subprocess.run(
                    [git_exe] + list(git_args), cwd=cwd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", startupinfo=startupinfo,
                    creationflags=creationflags, input=input_text, env=git_env,
                    timeout=max(10, int(timeout)),
                )
                return cp.returncode, cp.stdout.strip(), cp.stderr.strip()
            except subprocess.TimeoutExpired:
                return 124, "", f"Git non ha risposto entro {int(timeout)} secondi. Operazione interrotta in sicurezza."
            except FileNotFoundError:
                return 127, "", "Git integrato non disponibile."
            except Exception as exc:
                return 1, "", f"Impossibile avviare Git: {exc}"

        code, out, err = run_git(args, input_text=input_text)
        if code == 0:
            return code, out, err

        if "push" in args and ("403" in err or "Permission to" in err or "denied to" in err):
            try:
                u = urllib.parse.urlparse(self.remote)
                if u.scheme in ("http", "https") and u.hostname == "github.com":
                    cred = f"protocol={u.scheme}\nhost={u.hostname}\npath={u.path.lstrip('/')}\n\n"
                    run_git(["credential", "reject"], input_text=cred)
                    self.write_log("↻ Credenziale GitHub rifiutata: nuovo accesso richiesto per questo repository.")
                    return run_git(args)
            except Exception:
                pass
        return code, out, err

    def _github_host_credential(self):
        """Return (username, token) from GCM for GitHub, kept in memory only."""
        os.makedirs(APP_DIR, exist_ok=True)
        payload = "protocol=https\nhost=github.com\n\n"
        code, out, err = self.git(
            ["-c", "credential.https://github.com.useHttpPath=false", "credential", "fill"],
            cwd=APP_DIR,
            timeout=120,
            input_text=payload,
        )
        if code != 0:
            raise WorkspaceError(err or "Impossibile leggere la sessione GitHub da Git Credential Manager.")
        fields = {}
        for line in (out or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
        username = fields.get("username", "")
        token = fields.get("password", "")
        if not token:
            raise WorkspaceError("Sessione GitHub non disponibile. Esegui prima Accedi a GitHub.")
        return username, token

    def _github_api_json(self, api_url, token):
        """Authenticated GitHub API GET. The token is never persisted or logged."""
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": UPDATE_USER_AGENT,
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode("utf-8")), response.headers
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise WorkspaceError("Sessione GitHub scaduta o senza permessi sufficienti.") from exc
            raise WorkspaceError(f"GitHub API ha risposto HTTP {exc.code}.") from exc
        except Exception as exc:
            raise WorkspaceError(f"Impossibile comunicare con GitHub: {exc}") from exc

    def _github_account_and_repositories(self):
        """Login through GCM, then return account login and all accessible repositories."""
        self._github_browser_login(APP_DIR)
        _, token = self._github_host_credential()
        try:
            me, _ = self._github_api_json("https://api.github.com/user", token)
            login = str(me.get("login") or "")
            repos = []
            page = 1
            while page <= 10:
                url = (
                    "https://api.github.com/user/repos"
                    f"?per_page=100&page={page}&sort=updated"
                    "&affiliation=owner,collaborator,organization_member"
                )
                batch, _ = self._github_api_json(url, token)
                if not isinstance(batch, list):
                    raise WorkspaceError("Risposta repository GitHub non valida.")
                repos.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            cleaned = []
            seen = set()
            for item in repos:
                full_name = str(item.get("full_name") or "").strip()
                clone_url = str(item.get("clone_url") or "").strip()
                if not full_name or not clone_url or full_name.lower() in seen:
                    continue
                seen.add(full_name.lower())
                cleaned.append({
                    "full_name": full_name,
                    "clone_url": clone_url,
                    "private": bool(item.get("private", False)),
                    "default_branch": str(item.get("default_branch") or "main"),
                    "permissions": item.get("permissions") if isinstance(item.get("permissions"), dict) else {},
                })
            cleaned.sort(key=lambda x: x["full_name"].lower())
            return login, cleaned
        finally:
            token = None

    def _github_repository_branches(self, full_name):
        _, token = self._github_host_credential()
        try:
            quoted = "/".join(urllib.parse.quote(part, safe="") for part in full_name.split("/", 1))
            branches = []
            page = 1
            while page <= 5:
                url = f"https://api.github.com/repos/{quoted}/branches?per_page=100&page={page}"
                batch, _ = self._github_api_json(url, token)
                if not isinstance(batch, list):
                    raise WorkspaceError("Risposta branch GitHub non valida.")
                branches.extend(str(item.get("name") or "") for item in batch if item.get("name"))
                if len(batch) < 100:
                    break
                page += 1
            return sorted(set(branches), key=str.lower)
        finally:
            token = None

    def _github_repo_parts(self):
        try:
            u = urllib.parse.urlparse(self.remote)
            if u.scheme not in ("http", "https") or (u.hostname or "").lower() != "github.com":
                return None
            parts = [x for x in (u.path or "").strip("/").split("/") if x]
            if len(parts) < 2:
                return None
            owner, repo = parts[0], parts[1]
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            return owner, repo
        except Exception:
            return None

    def _assert_remote_private(self):
        """Verify the configured GitHub remote is reachable. Public repositories are allowed."""
        # Local bare repositories are used only by the source test-suite. Frozen
        # production builds never accept a local path as a sync remote.
        if not getattr(sys, "frozen", False) and self.remote and os.path.exists(self.remote):
            return
        if not self.remote:
            raise WorkspaceError("Repository GitHub non configurato. Impostalo nelle Impostazioni.")
        parts = self._github_repo_parts()
        if not parts:
            raise WorkspaceError("SchoolHub accetta per la sincronizzazione solo repository GitHub HTTPS.")
        owner, repo = parts
        api = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        req = urllib.request.Request(api, headers={"User-Agent": UPDATE_USER_AGENT, "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("private") is False:
                self.write_log("⚠ Repository GitHub pubblico: sincronizzazione consentita su richiesta dell'utente.")
                return
            if data.get("private") is True:
                return
        except WorkspaceError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise WorkspaceError(f"Impossibile verificare il repository GitHub (HTTP {exc.code}).") from exc
            # GitHub intentionally returns 404 for private repositories to anonymous API calls.
            os.makedirs(APP_DIR, exist_ok=True)
            code, out, err = self.git(["ls-remote", self._git_remote_for_auth()], cwd=APP_DIR, timeout=60)
            if code != 0:
                raise WorkspaceError(err or out or "Repository privato non raggiungibile o accesso GitHub non autorizzato.")
            return
        except Exception as exc:
            raise WorkspaceError(f"Impossibile verificare il repository GitHub: {exc}") from exc

    @staticmethod
    def _hash_tree(root):
        root = os.path.abspath(root)
        result = {}
        if not os.path.isdir(root):
            return result
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d != ".git" and not os.path.islink(os.path.join(current, d))]
            for name in files:
                path = os.path.join(current, name)
                if os.path.islink(path):
                    raise WorkspaceError(f"Collegamento simbolico non supportato nella sincronizzazione: {os.path.relpath(path, root)}")
                rel = os.path.relpath(path, root).replace("\\", "/")
                if rel == ".gitattributes":
                    continue
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                result[rel] = h.hexdigest()
        return result

    @staticmethod
    def _copy_tree(src, dst):
        os.makedirs(dst, exist_ok=True)
        src = os.path.abspath(src)
        dst = os.path.abspath(dst)
        for current, dirs, files in os.walk(src, followlinks=False):
            dirs[:] = [d for d in dirs if d != ".git" and not os.path.islink(os.path.join(current, d))]
            rel_dir = os.path.relpath(current, src)
            target_dir = dst if rel_dir == "." else os.path.join(dst, rel_dir)
            os.makedirs(target_dir, exist_ok=True)
            for name in files:
                source_file = os.path.join(current, name)
                rel_file = os.path.relpath(source_file, src).replace("\\", "/")
                if rel_file == ".gitattributes":
                    continue
                if os.path.islink(source_file):
                    raise WorkspaceError(f"Collegamento simbolico non supportato: {source_file}")
                shutil.copy2(source_file, os.path.join(target_dir, name))

    @staticmethod
    def _replace_tree(src, dst):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(dst):
            shutil.rmtree(os.path.join(dst, name), ignore_errors=True) if os.path.isdir(os.path.join(dst, name)) else os.unlink(os.path.join(dst, name))
        SchoolHub._copy_tree(src, dst)

    def _load_sync_state(self):
        if not os.path.exists(self.sync_state_file):
            return None
        try:
            with open(self.sync_state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
                raise ValueError("formato baseline non valido")
            return data
        except Exception as exc:
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(self.sync_state_file, self.sync_state_file + ".corrupt-" + stamp)
            except Exception:
                pass
            raise WorkspaceError(
                "Lo stato di sincronizzazione locale è danneggiato. È stata conservata una copia di diagnosi; "
                "non sincronizzo automaticamente per evitare sovrascritture."
            ) from exc

    def _save_sync_state(self, files):
        os.makedirs(os.path.dirname(self.sync_state_file), exist_ok=True)
        tmp = self.sync_state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "files": files}, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.sync_state_file)

    def _git_remote_for_auth(self):
        """Prefer the GitHub repository owner as the HTTPS username so each Windows user
        gets an isolated Credential Manager entry and GitHub can select the right account."""
        if not self.remote:
            return self.remote
        import urllib.parse, re
        try:
            u = urllib.parse.urlparse(self.remote)
            if u.scheme in ("http", "https") and u.hostname == "github.com":
                user = self.github_user
                if not user:
                    m = re.match(r"/([^/]+)/", u.path or "")
                    user = m.group(1) if m else ""
                if user:
                    return urllib.parse.urlunparse((u.scheme, f"{urllib.parse.quote(user)}@github.com", u.path, u.params, u.query, u.fragment))
        except Exception:
            pass
        return self.remote

    def _clone_remote(self, destination):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        code, out, err = self.git(
            ["clone", "--depth", "1", "--no-tags", self._git_remote_for_auth(), destination],
            cwd=os.path.dirname(destination)
        )
        if code != 0:
            raise WorkspaceError(err or out or "Impossibile scaricare il repository GitHub.")

        # Keep credentials separated by GitHub repository path on this Windows user.
        self.git(["config", "credential.useHttpPath", "true"], cwd=destination)

        # Check out the requested branch if it exists remotely. On an empty repo,
        # point HEAD to the requested unborn branch without requiring a commit.
        code, _, _ = self.git(["rev-parse", "--verify", f"refs/remotes/origin/{self.branch}"], cwd=destination)
        if code == 0:
            code, out, err = self.git(["checkout", "-B", self.branch, f"origin/{self.branch}"], cwd=destination)
            if code != 0:
                raise WorkspaceError(err or out or f"Impossibile aprire il branch {self.branch}.")
        else:
            code_head, _, _ = self.git(["rev-parse", "--verify", "HEAD"], cwd=destination)
            if code_head == 0:
                raise WorkspaceError(
                    f"Il branch '{self.branch}' non esiste su GitHub. Imposta nelle Impostazioni il branch corretto prima di sincronizzare."
                )
            code, out, err = self.git(["symbolic-ref", "HEAD", f"refs/heads/{self.branch}"], cwd=destination)
            if code != 0:
                raise WorkspaceError(err or out or f"Impossibile inizializzare il branch {self.branch}.")

    def _get_sync_source(self, password, temp_root):
        """Create a stable plaintext snapshot used for the whole sync operation."""
        source = os.path.join(temp_root, "workspace-snapshot")
        if self.workspace.is_unlocked:
            self._copy_tree(self.workspace.get_unlocked_path(), source)
            return source, True
        if not password:
            raise WorkspaceError("Il Workspace è bloccato. Inserisci la password per sincronizzare.")
        self.workspace.export_to(source, password)
        return source, True

    def _ensure_git_identity(self, repo):
        code, name, _ = self.git(["config", "user.name"], cwd=repo)
        if code != 0 or not name.strip():
            self.git(["config", "user.name", self.github_user or "SchoolHub Sync"], cwd=repo)
        code, email, _ = self.git(["config", "user.email"], cwd=repo)
        if code != 0 or not email.strip():
            user = (self.github_user or "schoolhub").replace(" ", "-")
            self.git(["config", "user.email", f"{user}@users.noreply.github.com"], cwd=repo)

    def _prepare_git_lfs(self, repo):
        """Enable Git LFS locally and track common audio/video formats."""
        code, out, err = self.git(["lfs", "version"], cwd=repo)
        if code != 0:
            raise WorkspaceError(
                "Git LFS integrato non disponibile: impossibile sincronizzare in sicurezza MP3/MP4 e altri media grandi. "
                + (err or out or "")
            )
        code, out, err = self.git(["lfs", "install", "--local"], cwd=repo)
        if code != 0:
            raise WorkspaceError(err or out or "Impossibile inizializzare Git LFS.")
        # SchoolHub does not use Git LFS file locking. The lock verification API
        # requires authenticated push access and can block an otherwise valid push.
        code, out, err = self.git(["config", "lfs.locksverify", "false"], cwd=repo)
        if code != 0:
            raise WorkspaceError(err or out or "Impossibile disattivare la verifica lock Git LFS.")
        code, out, err = self.git(["lfs", "track", *LFS_MEDIA_PATTERNS], cwd=repo)
        if code != 0:
            raise WorkspaceError(err or out or "Impossibile configurare Git LFS per i file multimediali.")
        self.write_log("✓ Git LFS attivo per MP3/MP4; verifica lock disattivata (SchoolHub non usa i lock).")

    @staticmethod
    def _looks_like_github_auth_error(text):
        value = (text or "").lower()
        markers = (
            "authentication required",
            "authentication failed",
            "could not read username",
            "repository not found",
            "permission to",
            "denied to",
            "write access",
            "push access",
            "verify locks",
            "http 401",
            "http 403",
            "status code 401",
            "status code 403",
        )
        return any(marker in value for marker in markers)

    def _github_browser_login(self, repo):
        """Force a fresh GitHub.com OAuth login using bundled GCM."""
        args = [
            "credential-manager", "github", "login",
            "--url", "https://github.com",
            "--web",
            "--force",
        ]
        self.write_log("🔐 GitHub richiede autenticazione: apro il login nel browser...")
        code, out, err = self.git(args, cwd=repo, timeout=300)
        if code != 0:
            raise WorkspaceError(
                "Accesso GitHub non completato. Accedi nel browser con un account che abbia permesso di scrittura sul repository. "
                + (err or out or "")
            )
        self.write_log("✓ Accesso GitHub completato. Riprovo il push.")

    def _push_with_auth_retry(self, repo, branch_name=None):
        """Push once, authenticate via GCM on auth failures, then retry exactly once."""
        target_branch = branch_name or self.branch
        args = ["push", "-u", "origin", target_branch]
        code, out, err = self.git(args, cwd=repo)
        if code == 0:
            return out

        detail = "\n".join(x for x in (out, err) if x).strip()
        if self._looks_like_github_auth_error(detail):
            self._github_browser_login(repo)
            code, out, err = self.git(args, cwd=repo)
            if code == 0:
                return out
            detail = "\n".join(x for x in (out, err) if x).strip()
            if self._looks_like_github_auth_error(detail):
                raise WorkspaceError(
                    "GitHub ha rifiutato il push anche dopo il login. "
                    "L'account autenticato deve avere permesso WRITE/PUSH sul repository configurato. "
                    + detail
                )

        raise WorkspaceError(detail or "git push fallito.")

    @staticmethod
    def _remove_path(path):
        if os.path.islink(path) or os.path.isfile(path):
            try: os.unlink(path)
            except FileNotFoundError: pass
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=False)

    @staticmethod
    def _ensure_parent_dirs(root, rel):
        parts = rel.replace("\\", "/").split("/")[:-1]
        current = root
        for part in parts:
            current = os.path.join(current, part)
            if os.path.lexists(current) and (os.path.islink(current) or not os.path.isdir(current)):
                SchoolHub._remove_path(current)
            os.makedirs(current, exist_ok=True)

    def _apply_paths(self, source_root, dest_root, source_files, paths):
        """Apply selected file additions/changes/deletions, handling file↔directory changes."""
        for rel in sorted(set(paths), key=lambda x: (x.count("/"), x)):
            src = os.path.join(source_root, *rel.split("/"))
            dst = os.path.join(dest_root, *rel.split("/"))
            if rel not in source_files:
                if os.path.lexists(dst):
                    self._remove_path(dst)
                continue
            self._ensure_parent_dirs(dest_root, rel)
            if os.path.lexists(dst):
                self._remove_path(dst)
            shutil.copy2(src, dst)

        # Remove now-empty directories, excluding .git.
        for current, dirs, files in os.walk(dest_root, topdown=False):
            if os.path.basename(current) == ".git" or ".git" in current.split(os.sep):
                continue
            if current == dest_root:
                continue
            try:
                if not os.listdir(current): os.rmdir(current)
            except OSError:
                pass

    def _persist_sync_tree(self, source, password, temp_root, remote_paths=None):
        """Persist merged user files without replacing the whole live Workspace."""
        user_tree = os.path.join(temp_root, "merged-user-tree")
        if os.path.exists(user_tree):
            shutil.rmtree(user_tree, ignore_errors=True)
        self._copy_tree(source, user_tree)
        expected = self._hash_tree(user_tree)
        if self.workspace.is_unlocked:
            remote_paths = list(remote_paths or [])
            if remote_paths:
                self.workspace.apply_plaintext_paths_from_tree(user_tree, remote_paths)
            self.workspace.save_unlocked_to_vault(user_tree)
            actual = self._hash_tree(self.workspace.get_unlocked_path())
        else:
            if not password:
                raise WorkspaceError("Password Workspace necessaria per applicare la sincronizzazione.")
            self.workspace.import_from(user_tree, password)
            verify_dir = os.path.join(temp_root, "verify-vault")
            if os.path.exists(verify_dir): shutil.rmtree(verify_dir, ignore_errors=True)
            self.workspace.export_to(verify_dir, password)
            actual = self._hash_tree(verify_dir)
        if actual != expected:
            raise WorkspaceError("Verifica finale fallita: il Vault/Workspace non corrisponde ai file sincronizzati.")

    def _verify_remote_head(self, repo):
        """After a push, verify that origin/<branch> points to the same commit."""
        code, local_head, _ = self.git(["rev-parse", "HEAD"], cwd=repo)
        if code != 0 or not local_head:
            return  # empty repository: there is no commit to verify
        code, out, err = self.git(["ls-remote", "origin", f"refs/heads/{self.branch}"], cwd=repo)
        if code != 0:
            raise WorkspaceError(err or "Impossibile verificare il commit pubblicato su GitHub.")
        remote_head = out.split()[0] if out.split() else ""
        if remote_head != local_head:
            raise WorkspaceError("GitHub non conferma il commit appena inviato. La sincronizzazione non viene marcata come completata.")

    def _classify_changes(self, baseline, local_files, remote_files):
        """Three-way file comparison. A conflict exists only when the same path
        changed differently on both sides. Independent changes are merged automatically."""
        all_paths = set(baseline) | set(local_files) | set(remote_files)
        local_only, remote_only, conflicts = [], [], []
        for path in sorted(all_paths):
            b = baseline.get(path)
            l = local_files.get(path)
            r = remote_files.get(path)
            lc = l != b
            rc = r != b
            if lc and rc:
                if l == r:
                    continue
                conflicts.append(path)
            elif lc:
                local_only.append(path)
            elif rc:
                remote_only.append(path)
        return local_only, remote_only, conflicts

    def get_status(self):
        if not self.git_enabled or not self.remote:
            return None, None, "GitHub non configurato: SchoolHub funziona in locale."
        if not self.workspace.exists:
            return None, None, "Workspace cifrato non trovato."
        if not self.workspace.is_unlocked:
            return None, None, "Workspace bloccato: sbloccalo per controllare lo stato."
        temp_root = tempfile.mkdtemp(prefix="schoolhub-status-")
        repo = os.path.join(temp_root, "repo")
        try:
            self._assert_remote_private()
            self._clone_remote(repo)
            local_files = self._hash_tree(self.workspace.get_unlocked_path())
            remote_files = self._hash_tree(repo)
            state = self._load_sync_state()
            baseline = state.get("files", {}) if state is not None else {}
            local_only, remote_only, conflicts = self._classify_changes(baseline, local_files, remote_files)
            self.last_conflicts = conflicts[:100]
            return len(local_only) + len(conflicts), len(remote_only) + len(conflicts), bool(local_only or remote_only or conflicts)
        except Exception as e:
            return None, None, str(e)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def start_sync(self, manual=True):
        if not self.git_enabled or not self.remote:
            if manual:
                messagebox.showwarning("GitHub non configurato", "SchoolHub funziona in locale. Per sincronizzare, configura nelle Impostazioni un repository GitHub PRIVATO.", parent=self.root)
            return
        if self.sync_running:
            if manual: messagebox.showinfo("SchoolHub", "Una sincronizzazione è già in corso.", parent=self.root)
            return
        if self.workspace_busy:
            if manual: messagebox.showinfo("SchoolHub", "È in corso un'operazione sul Workspace. Attendi che termini.", parent=self.root)
            else: self.write_log("◷ Sync automatica saltata: Workspace occupato.")
            return
        if not self.workspace.exists:
            if manual: messagebox.showwarning("Workspace", "Crea prima il Workspace cifrato.", parent=self.root)
            else: self.write_log("◷ Sync automatica saltata: Workspace non ancora creato.")
            return

        password = None
        if not self.workspace.is_unlocked:
            if not manual:
                self.write_log("◷ Sync automatica saltata: Workspace bloccato.")
                return
            password = self.ask_password("Sincronizza Workspace", "Inserisci la password per leggere temporaneamente il Vault durante la sincronizzazione.")
            if password is None: return

        self.sync_running = True
        self.open_progress("Sincronizzazione SchoolHub", "Workspace cifrato ↔ repository GitHub")
        self.progress_update(2, "Preparazione", "Creazione snapshot sicuro")
        self.set_status("SINCRONIZZAZIONE...", YELLOW, "Workspace cifrato ↔ GitHub")
        threading.Thread(target=self.sync_worker, args=(password,), daemon=True).start()

    def sync_worker(self, password=None):
        temp_root = tempfile.mkdtemp(prefix="schoolhub-sync-")
        repo = os.path.join(temp_root, "repo")
        try:
            self.progress_update(5, "Snapshot", "Preparazione dei file locali")
            source, _ = self._get_sync_source(password, temp_root)
            self.progress_update(14, "Analisi", "Calcolo impronte SHA-256")
            local_files = self._hash_tree(source)
            state = self._load_sync_state()

            self.progress_update(20, "Sicurezza", "Verifica repository GitHub")
            self._assert_remote_private()
            self.write_log("↔ Scaricamento repository GitHub in area temporanea...")
            self.progress_update(28, "Download GitHub", "Clone shallow del repository")
            self._clone_remote(repo)
            self.progress_update(43, "Confronto", "Analisi differenze locale ↔ GitHub")
            remote_files = self._hash_tree(repo)

            baseline = state.get("files", {}) if state is not None else {}
            local_only, remote_only, conflicts = self._classify_changes(baseline, local_files, remote_files)
            self.last_conflicts = conflicts[:100]

            if conflicts:
                prefix = "Prima sincronizzazione: " if state is None else ""
                raise WorkspaceError(prefix + f"conflitto su {len(conflicts)} file. Workspace e GitHub contengono versioni diverse degli stessi percorsi. Apri Conflitti e scegli quale versione mantenere.")

            if self.workspace.is_unlocked and self._hash_tree(self.workspace.get_unlocked_path()) != local_files:
                raise WorkspaceError("Il Workspace è stato modificato mentre la sincronizzazione era in corso. Nessun file viene sovrascritto: avvia di nuovo la sync.")

            if not local_only and not remote_only:
                if state is None or baseline != remote_files:
                    self._save_sync_state(remote_files)
                self.write_log("✓ Repository già sincronizzato." if state is not None else "✓ Prima sincronizzazione: Workspace e GitHub sono già identici.")
                self.progress_update(100, "Completato", "Tutto aggiornato")
                self.finish_status("SINCRONIZZATO", GREEN, "Tutto aggiornato")
                time.sleep(0.25)
                return

            self.progress_update(52, "Merge", f"{len(local_only)} locali · {len(remote_only)} remoti")
            if local_only:
                self._apply_paths(source, repo, local_files, local_only)

            self.progress_update(64, "Aggiornamento Vault", "Cifratura risultato · aggiornamento solo file remoti")
            self._persist_sync_tree(repo, password, temp_root, remote_only)

            if local_only:
                self.progress_update(78, "Commit", "Preparazione modifiche Git + LFS media")
                self._ensure_git_identity(repo)
                self._prepare_git_lfs(repo)
                code, _, err = self.git(["add", "-A"], cwd=repo)
                if code != 0: raise WorkspaceError(err or "git add fallito.")
                message = "SchoolHub initial sync" if state is None else "SchoolHub automatic sync"
                code, out, err = self.git(["commit", "-m", message], cwd=repo)
                if code != 0 and "nothing to commit" not in (out + " " + err).lower():
                    raise WorkspaceError(err or out or "git commit fallito.")
                self.progress_update(86, "Upload GitHub", "Invio modifiche e media LFS al repository")
                self._push_with_auth_retry(repo)
                self.progress_update(93, "Verifica remota", "Controllo commit pubblicato")
                self._verify_remote_head(repo)

            final_files = self._hash_tree(repo)
            self.progress_update(96, "Verifica finale", "Confronto byte per byte tramite SHA-256")
            if self.workspace.is_unlocked:
                if self._hash_tree(self.workspace.get_unlocked_path()) != final_files:
                    raise WorkspaceError("Verifica finale fallita: Workspace e repository temporaneo non coincidono.")
            else:
                verify_dir = os.path.join(temp_root, "verify-final")
                self.workspace.export_to(verify_dir, password)
                if self._hash_tree(verify_dir) != final_files:
                    raise WorkspaceError("Verifica finale fallita: Vault cifrato e repository temporaneo non coincidono.")

            self._save_sync_state(final_files)
            self.last_conflicts = []
            if local_only and remote_only:
                self.write_log(f"↔ Sync completata: {len(local_only)} modifiche locali + {len(remote_only)} modifiche GitHub unite senza conflitti.")
            elif local_only:
                self.write_log(f"↑ {len(local_only)} modifiche locali sincronizzate su GitHub.")
            else:
                self.write_log(f"↓ {len(remote_only)} modifiche GitHub importate nel Vault.")
            self.progress_update(100, "Completato", "Sincronizzazione completata")
            self.finish_status("SINCRONIZZATO", GREEN, "Sincronizzazione completata")
            time.sleep(0.25)
        except Exception as e:
            self.write_log(f"✕ ERRORE SYNC: {e}")
            self.progress_update(100, "Errore", str(e))
            if self.last_conflicts: self.finish_status("CONFLITTO", RED, str(e))
            else: self.finish_status("ERRORE", RED, str(e))
            time.sleep(0.6)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            self.sync_running = False
            self.close_progress()
            if self.running:
                self.root.after(0, self.refresh_status)

    def initial_status(self):

        self.refresh_status()

    def refresh_status(self):

        generation = self.page_generation

        def worker():

            local, remote, changes = self.get_status()

            if local is None:
                return

            if not self.running:
                return

            self.root.after(
                0,
                lambda:
                self.update_counters(
                    local,
                    remote,
                    changes,
                    generation
                )
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    def update_counters(
        self,
        local,
        remote,
        changes,
        generation=None
    ):

        if not self.running:
            return

        if (
            generation is not None
            and
            generation != self.page_generation
        ):
            return

        widgets = [
            (
                getattr(
                    self,
                    "local_value",
                    None
                ),
                str(local)
            ),
            (
                getattr(
                    self,
                    "github_value",
                    None
                ),
                str(remote)
            ),
            (
                getattr(
                    self,
                    "last_value",
                    None
                ),
                datetime.now().strftime(
                    "%H:%M:%S"
                )
            )
        ]

        for widget, value in widgets:

            if not self.widget_alive(widget):
                continue

            try:

                widget.configure(
                    text=value
                )

            except tk.TclError:
                pass

        if changes and local == 0 and remote == 0:

            self.set_status(
                "MODIFICHE LOCALI",
                YELLOW,
                "Modifiche da sincronizzare"
            )

        elif local == 0 and remote == 0:

            self.set_status(
                "SINCRONIZZATO",
                GREEN,
                "Tutto aggiornato"
            )

        elif local > 0 and remote == 0:

            self.set_status(
                "DA INVIARE",
                YELLOW,
                f"{local} file locali"
            )

        elif local == 0 and remote > 0:

            self.set_status(
                "DA SCARICARE",
                YELLOW,
                f"{remote} file da GitHub"
            )

        else:

            self.set_status(
                "CONFLITTO",
                RED,
                "Locale e GitHub divergenti"
            )

    # ========================================================
    # STATUS
    # ========================================================

    def set_status(
        self,
        title,
        color,
        detail=""
    ):

        generation = self.page_generation

        def update():

            if not self.running:
                return

            if generation != self.page_generation:
                return

            try:

                status_text = getattr(
                    self,
                    "status_text",
                    None
                )

                if self.widget_alive(status_text):

                    status_text.configure(
                        text=title,
                        fg=color
                    )

                status_dot = getattr(
                    self,
                    "status_dot",
                    None
                )

                if self.widget_alive(status_dot):

                    status_dot.configure(
                        fg=color
                    )

                status_detail = getattr(
                    self,
                    "status_detail",
                    None
                )

                if self.widget_alive(status_detail):

                    status_detail.configure(
                        text=detail
                    )

            except tk.TclError:
                pass

        if self.running:

            self.root.after(
                0,
                update
            )

    def finish_status(
        self,
        title,
        color,
        detail
    ):

        self.set_status(
            title,
            color,
            detail
        )

    # ========================================================
    # AUTO SYNC
    # ========================================================

    def schedule_auto_sync(self):
        if self.auto_sync_job is not None:
            try:
                self.root.after_cancel(self.auto_sync_job)
            except tk.TclError:
                pass
            self.auto_sync_job = None

        if self.running and self.auto_sync_enabled and self.git_enabled and self.remote:
            self.auto_sync_job = self.root.after(
                max(30, int(self.interval)) * 1000,
                self.auto_sync
            )

    def _auto_sync_after_unlock(self):
        if not self.running:
            return
        if not (self.auto_sync_enabled and self.git_enabled and self.remote):
            return
        if self.sync_running or self.workspace_busy or not self.workspace.is_unlocked:
            return
        self.auto_sync_pending_unlock = False
        self.write_log("◷ Workspace sbloccato: eseguo la sincronizzazione automatica in attesa.")
        self.start_sync(manual=False)

    def auto_sync(self):
        self.auto_sync_job = None
        if not self.running:
            return

        if self.auto_sync_enabled and self.git_enabled and self.remote:
            if self.sync_running or self.workspace_busy:
                self.write_log("◷ Sync automatica rimandata: SchoolHub occupato.")
            elif not self.workspace.is_unlocked:
                self.auto_sync_pending_unlock = True
                self.write_log("◷ Sync automatica in attesa: sblocca il Workspace; partirà subito dopo.")
            else:
                self.auto_sync_pending_unlock = False
                self.write_log("◷ Avvio sincronizzazione automatica.")
                self.start_sync(manual=False)

        self.schedule_auto_sync()

    def set_windows_startup(self, enabled):
        """Apply and verify HKCU Run startup entry. Return True only when verified."""
        if os.name != "nt":
            return not enabled
        try:
            import winreg

            legacy = os.path.join(
                os.environ.get("APPDATA", ""),
                r"Microsoft\Windows\Start Menu\Programs\Startup\SchoolHub.lnk"
            )
            try:
                if os.path.isfile(legacy):
                    os.unlink(legacy)
            except OSError:
                pass

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            value_name = "SchoolHub"
            expected = None
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            ) as key:
                if enabled:
                    if getattr(sys, "frozen", False):
                        exe = os.path.abspath(sys.executable)
                        if not os.path.isfile(exe):
                            raise RuntimeError(f"EXE SchoolHub non trovato: {exe}")
                        expected = f'"{exe}"'
                    else:
                        python = sys.executable
                        if os.path.basename(python).lower() == "python.exe":
                            pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
                            if os.path.exists(pythonw):
                                python = pythonw
                        expected = f'"{python}" "{os.path.abspath(__file__)}"'
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, expected)
                    winreg.FlushKey(key)
                    actual, _ = winreg.QueryValueEx(key, value_name)
                    if str(actual).strip() != expected:
                        raise RuntimeError("Windows non ha confermato il comando di avvio automatico.")
                    self.write_log(f"✓ Avvio automatico Windows verificato: {expected}")
                    return True

                try:
                    winreg.DeleteValue(key, value_name)
                    winreg.FlushKey(key)
                except FileNotFoundError:
                    pass
                try:
                    winreg.QueryValueEx(key, value_name)
                    raise RuntimeError("Windows mantiene ancora la voce di avvio automatico.")
                except FileNotFoundError:
                    self.write_log("✓ Avvio automatico Windows disattivato e verificato.")
                    return True
        except Exception as exc:
            self.write_log(f"✕ Avvio automatico Windows non applicato: {exc}")
            return False

    def write_log(self, text):

        timestamp = datetime.now().strftime(
            "%d/%m/%Y   %H:%M"
        )

        line = f"[{timestamp}] {text}\n"

        try:

            os.makedirs(
                APP_DIR,
                exist_ok=True
            )

            with open(
                LOG_FILE,
                "a",
                encoding="utf-8"
            ) as f:

                f.write(line)

        except Exception:
            pass

    def load_recent_log(self):

        box = getattr(
            self,
            "activity_box",
            None
        )

        if not self.widget_alive(box):
            return

        if not os.path.exists(
            LOG_FILE
        ):
            return

        try:

            with open(
                LOG_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                lines = f.readlines()

            lines = lines[-100:]

            box.configure(
                state="normal"
            )

            box.delete(
                "1.0",
                "end"
            )

            box.insert(
                "end",
                "".join(lines)
            )

            box.configure(
                state="disabled"
            )

        except Exception:
            pass

    # ========================================================
    # HELPERS
    # ========================================================

    def section_title(
        self,
        title,
        subtitle
    ):

        tk.Label(
            self.content,
            text=title,
            font=("Segoe UI", 18, "bold"),
            fg=TEXT,
            bg=BG
        ).pack(
            anchor="w",
            pady=(5, 2)
        )

        tk.Label(
            self.content,
            text=subtitle,
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=BG
        ).pack(
            anchor="w"
        )

    def info_row(
        self,
        parent,
        key,
        value
    ):

        row = tk.Frame(
            parent,
            bg=PANEL
        )

        row.pack(
            fill="x",
            padx=22,
            pady=9
        )

        tk.Label(
            row,
            text=key,
            width=18,
            anchor="w",
            font=("Segoe UI", 9, "bold"),
            fg=MUTED,
            bg=PANEL
        ).pack(
            side="left"
        )

        tk.Label(
            row,
            text=value,
            font=("Segoe UI", 9),
            fg=TEXT,
            bg=PANEL
        ).pack(
            side="left"
        )

    # ========================================================
    # CLOCK
    # ========================================================

    def update_clock(self):

        if not self.running:
            return

        if self.widget_alive(
            getattr(
                self,
                "time_label",
                None
            )
        ):

            try:

                self.time_label.configure(
                    text=datetime.now().strftime(
                        "%d/%m/%Y   %H:%M"
                    )
                )

            except tk.TclError:
                return

        if self.running:

            self.root.after(60000, self.update_clock)

    # ========================================================
    # CLOSE
    # ========================================================

    def close(self):
        if self.sync_running or self.workspace_busy:
            messagebox.showwarning(
                "SchoolHub",
                "È in corso una sincronizzazione o un'operazione di cifratura. Per proteggere i file SchoolHub non verrà chiuso finché l'operazione non termina."
            )
            return
        if self.workspace.is_unlocked:
            answer = messagebox.askyesno(
                "Workspace sbloccato",
                "Il Workspace è ancora sbloccato e contiene file leggibili.\n\nVuoi chiudere comunque senza bloccarlo?"
            )
            if not answer:
                return
        self.running = False
        if self.auto_sync_job is not None:
            try: self.root.after_cancel(self.auto_sync_job)
            except tk.TclError: pass
            self.auto_sync_job = None
        self.root.destroy()



# ============================================================
# ENTRY POINT / FROZEN BUILD SELF-TEST
# ============================================================

def _frozen_self_test(output_path):
    """Small non-GUI smoke test used by the Windows build pipeline."""
    result = {"ok": False, "checks": {}, "error": None}
    temp_root = None
    try:
        temp_root = tempfile.mkdtemp(prefix="schoolhub-frozen-selftest-")
        git_exe, git_env, bundled = bundled_git_info()
        if not git_exe:
            raise RuntimeError("Git non disponibile")
        cp = subprocess.run(
            [git_exe, "--version"], cwd=temp_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=git_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr or cp.stdout or "git --version fallito")
        result["checks"]["git"] = cp.stdout.strip()
        result["checks"]["git_bundled"] = bool(bundled)
        cp_gcm = subprocess.run(
            [git_exe, "credential-manager", "--version"], cwd=temp_root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=git_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if cp_gcm.returncode != 0:
            raise RuntimeError(cp_gcm.stderr or cp_gcm.stdout or "Git Credential Manager non disponibile")
        result["checks"]["git_credential_manager"] = cp_gcm.stdout.strip() or cp_gcm.stderr.strip() or True

        cp_login_help = subprocess.run(
            [git_exe, "credential-manager", "github", "login", "--help"], cwd=temp_root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=git_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if cp_login_help.returncode != 0 or "--force" not in (cp_login_help.stdout + cp_login_help.stderr):
            raise RuntimeError("Comando login GitHub GCM non disponibile")
        result["checks"]["github_browser_login_command"] = True

        cp_lfs = subprocess.run(
            [git_exe, "lfs", "version"], cwd=temp_root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=git_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if cp_lfs.returncode != 0:
            raise RuntimeError(cp_lfs.stderr or cp_lfs.stdout or "Git LFS non disponibile")
        result["checks"]["git_lfs"] = cp_lfs.stdout.strip() or cp_lfs.stderr.strip() or True

        # Functional LFS test: both MP3 and MP4 must enter the index as LFS objects.
        lfs_repo = os.path.join(temp_root, "lfs-repo")
        os.makedirs(lfs_repo, exist_ok=True)
        def run_lfs_git(args):
            cp = subprocess.run(
                [git_exe] + args, cwd=lfs_repo, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=git_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            if cp.returncode != 0:
                raise RuntimeError(cp.stderr or cp.stdout or "Git LFS self-test fallito")
            return cp.stdout.strip()
        run_lfs_git(["init"])
        run_lfs_git(["lfs", "install", "--local"])
        run_lfs_git(["config", "lfs.locksverify", "false"])
        if run_lfs_git(["config", "--get", "lfs.locksverify"]).strip().lower() != "false":
            raise RuntimeError("Git LFS locksverify non disattivato")
        result["checks"]["lfs_locks_disabled"] = True
        run_lfs_git(["lfs", "track", "*.mp3", "*.mp4"])
        with open(os.path.join(lfs_repo, "audio.mp3"), "wb") as fh:
            fh.write(b"ID3" + os.urandom(1024 * 1024))
        with open(os.path.join(lfs_repo, "video.mp4"), "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(1024 * 1024))
        run_lfs_git(["add", ".gitattributes", "audio.mp3", "video.mp4"])
        listed = run_lfs_git(["lfs", "ls-files"])
        if "audio.mp3" not in listed or "video.mp4" not in listed:
            raise RuntimeError("MP3/MP4 non vengono gestiti da Git LFS")
        result["checks"]["media_git_lfs"] = True

        ws = os.path.join(temp_root, "Workspaces", "Scuola")
        vault = os.path.join(temp_root, "Vaults", "Scuola.vault")
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "selftest.txt"), "w", encoding="utf-8") as fh:
            fh.write("SchoolHub self-test")
        with open(os.path.join(ws, "selftest.mp4"), "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + (b"SchoolHubMedia" * 400000))
        wm = WorkspaceManager(ws, vault)

        long_rel = os.path.join(
            "PCTO", "Corso Smart Learning", "Video Smart Learning",
            "ElevenLabs_2026-05-12T13_32_03_Manuela - Warm, Energetic and Swift_pvc_sp95_s27_sb100_se58_b_m2.mp3",
        )
        long_plain = wm._io_path(os.path.join(ws, long_rel))
        long_plain.parent.mkdir(parents=True, exist_ok=True)
        with long_plain.open("wb") as fh:
            fh.write(b"ID3" + (b"LongPathSchoolHub" * 20000))
        # Match the real bug: source path is still usable, but adding the
        # syncvault staging suffix + old temp suffix would exceed MAX_PATH.
        source_len = len(os.path.abspath(os.path.join(ws, long_rel)))
        simulated_old_tmp = os.path.join(
            vault + ".syncvault-1234567890abcdef", "files",
            long_rel + ".tmp-1234567890abcdef",
        )
        if source_len >= 260:
            raise RuntimeError(f"Long-path self-test sorgente troppo lungo ({source_len})")
        if len(os.path.abspath(simulated_old_tmp)) <= 260:
            raise RuntimeError("Long-path self-test non riproduce il vecchio tmp oltre MAX_PATH")

        password = "SchoolHub-SelfTest-Only-42!"
        wm.create(password)
        media_enc = os.path.join(vault, "files", "selftest.mp4")
        with open(media_enc, "rb") as fh:
            if fh.read(6) != b"SHENC2":
                raise RuntimeError("Formato streaming SHENC2 non attivo")

        # Legacy SHENC1 regression test: emulate a large old-format file and
        # verify that 2.4.2 can stream-decrypt it instead of loading it all at once.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        legacy_rel = "legacy-large.bin"
        legacy_plain = (b"LegacySchoolHub" * 600000)  # > 8 MiB
        legacy_key = wm._verify_password(password)
        legacy_nonce = os.urandom(wm.NONCE_LEN)
        legacy_aad = wm.FILE_AAD_PREFIX + legacy_rel.encode("utf-8")
        legacy_cipher = AESGCM(legacy_key).encrypt(legacy_nonce, legacy_plain, legacy_aad)
        legacy_path = os.path.join(vault, "files", legacy_rel)
        with open(legacy_path, "wb") as fh:
            fh.write(wm.FILE_MAGIC_V1)
            fh.write(legacy_nonce)
            fh.write(legacy_cipher)

        wm.unlock(password)
        with open(os.path.join(ws, legacy_rel), "rb") as fh:
            if fh.read() != legacy_plain:
                raise RuntimeError("Legacy SHENC1 streaming unlock non valido")
        result["checks"]["legacy_stream_unlock"] = True
        with open(os.path.join(ws, "selftest.txt"), "r", encoding="utf-8") as fh:
            if fh.read() != "SchoolHub self-test":
                raise RuntimeError("Vault round-trip non valido")
        if os.path.getsize(os.path.join(ws, "selftest.mp4")) < 4_000_000:
            raise RuntimeError("Round-trip media non valido")
        long_restored = wm._io_path(os.path.join(ws, long_rel))
        if not long_restored.is_file() or long_restored.stat().st_size < 100000:
            raise RuntimeError("Round-trip percorso lungo MP3 non valido")
        # This is the operation that failed in the user's log: unlocked sync
        # encrypting into Scuola.vault.syncvault-... with a long MP3 path.
        wm.save_unlocked_to_vault(ws)
        long_enc = wm._io_path(os.path.join(vault, "files", long_rel))
        if not long_enc.is_file():
            raise RuntimeError("Salvataggio syncvault percorso lungo MP3 non valido")
        result["checks"]["windows_long_media_path"] = True

        wm.session_lock()
        if wm.is_unlocked or not wm.has_plaintext:
            raise RuntimeError("Blocco rapido non valido")
        fast_started = time.monotonic()
        wm.unlock(password)
        fast_elapsed = time.monotonic() - fast_started
        if not wm.is_unlocked:
            raise RuntimeError("Sblocco rapido non valido")
        result["checks"]["instant_unlock_seconds"] = round(fast_elapsed, 3)

        marker_file = os.path.join(ws, "editor-open-marker.txt")
        with open(marker_file, "w", encoding="utf-8") as fh:
            fh.write("old")
        source_tree = os.path.join(temp_root, "remote-merge")
        shutil.copytree(ws, source_tree)
        with open(os.path.join(source_tree, "editor-open-marker.txt"), "w", encoding="utf-8") as fh:
            fh.write("new")
        workspace_before = wm.get_unlocked_path()
        stat_before = os.stat(workspace_before)
        wm.apply_plaintext_paths_from_tree(source_tree, ["editor-open-marker.txt"])
        workspace_after = wm.get_unlocked_path()
        stat_after = os.stat(workspace_after)
        if (stat_before.st_dev, stat_before.st_ino) != (stat_after.st_dev, stat_after.st_ino):
            raise RuntimeError("La sync incrementale ha sostituito la cartella Workspace")
        with open(marker_file, "r", encoding="utf-8") as fh:
            if fh.read() != "new":
                raise RuntimeError("Aggiornamento incrementale file remoto non valido")
        result["checks"]["editor_safe_incremental_sync"] = True

        wm.lock(password)
        result["checks"]["vault_roundtrip"] = True
        result["checks"]["media_streaming"] = True
        result["checks"]["version"] = APP_VERSION
        result["checks"]["safe_default_remote"] = (DEFAULT_REMOTE == "")
        if not result["checks"]["safe_default_remote"]:
            raise RuntimeError("Repository dati predefinito non sicuro")
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
        except Exception:
            pass
    return 0 if result["ok"] else 2


_INSTANCE_LOCK = None

def _acquire_single_instance():
    """Hold a 1-byte Windows file lock for the lifetime of the process."""
    global _INSTANCE_LOCK
    if os.name != "nt":
        return True
    fh = None
    try:
        import msvcrt
        os.makedirs(APP_DIR, exist_ok=True)
        lock_path = os.path.join(APP_DIR, "SchoolHub.instance.lock")
        fh = open(lock_path, "a+b")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _INSTANCE_LOCK = fh
        return True
    except OSError:
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass
        return False


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        raise SystemExit(_frozen_self_test(sys.argv[2]))

    if not _acquire_single_instance():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "SchoolHub è già aperto.", "SchoolHub", 0x40)
        except Exception:
            pass
        return

    root = None
    try:
        root = tk.Tk()
        SchoolHub(root)
        root.mainloop()
    except Exception as exc:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] CRASH AVVIO: {type(exc).__name__}: {exc}\n")
        except Exception:
            pass
        try:
            if root is None:
                root = tk.Tk(); root.withdraw()
            messagebox.showerror(
                "SchoolHub - Errore di avvio",
                f"SchoolHub non è riuscito ad avviarsi.\n\n{type(exc).__name__}: {exc}\n\nDettagli: {LOG_FILE}"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
