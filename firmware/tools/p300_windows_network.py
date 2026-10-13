"""Windows network helpers. Credential access is explicit; never log returned secrets."""
from __future__ import annotations
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

class NetworkUnavailableError(RuntimeError):
    pass

def open_hotspot_settings():
    if sys.platform != "win32":
        raise NetworkUnavailableError("共享助手仅适用于 Windows")
    os.startfile("ms-settings:network-mobilehotspot")

def network_interfaces() -> list[dict]:
    if sys.platform != "win32":
        return []
    script = (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Get-NetAdapter | ForEach-Object { "
        "$a=$_; $ips=@(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254.*'} | "
        "Select-Object -ExpandProperty IPAddress); if ($ips.Count -gt 0) { "
        "[pscustomobject]@{name=$a.Name;description=$a.InterfaceDescription;status=[string]$a.Status;"
        "addresses=$ips;hotspot_candidate=($a.InterfaceDescription -like '*Wi-Fi Direct*')} } } | ConvertTo-Json -Compress"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=15, creationflags=0x08000000)
    if result.returncode:
        raise NetworkUnavailableError("无法枚举网络接口")
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]

def parse_personal_wifi_profile(xml: str) -> dict:
    document = ET.fromstring(xml)
    def value(path):
        node = document.find(path)
        return node.text if node is not None else None
    ssid = value(".//{*}SSID/{*}name")
    one_x = value(".//{*}authEncryption/{*}useOneX")
    auth = value(".//{*}authEncryption/{*}authentication")
    if one_x == "true" or auth not in ("WPA2PSK", "WPAPSK", "WPA3SAE", "WPA3SAE-EXT"):
        raise NetworkUnavailableError("当前网络不是可直接复制的个人密码 Wi-Fi；校园网请使用 PC 热点或串口")
    protected = value(".//{*}sharedKey/{*}protected")
    password = value(".//{*}sharedKey/{*}keyMaterial")
    if not ssid or not password or protected != "false":
        raise NetworkUnavailableError("Windows 未允许读取当前网络密码，请手填一次或使用热点设置")
    return {"ssid": ssid, "password": password}

def current_personal_wifi() -> dict:
    """Invoke only after the user explicitly chooses to copy the current network."""
    if sys.platform != "win32":
        raise NetworkUnavailableError("Windows WLAN API unavailable")
    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte*8)]
    class Interface(ctypes.Structure):
        _fields_ = [("guid", GUID), ("description", ctypes.c_wchar*256), ("state", ctypes.c_uint32)]
    wlan = ctypes.WinDLL("wlanapi.dll")
    handle = ctypes.c_void_p()
    version = ctypes.c_uint32()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)):
        raise NetworkUnavailableError("WLAN service unavailable")
    interfaces = ctypes.c_void_p()
    try:
        if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)):
            raise NetworkUnavailableError("无法读取 WLAN 接口")
        count = ctypes.c_uint32.from_address(interfaces.value).value
        for i in range(count):
            interface = Interface.from_address(interfaces.value+8+i*ctypes.sizeof(Interface))
            if interface.state != 1:
                continue
            attributes = ctypes.c_void_p()
            size = ctypes.c_uint32()
            if wlan.WlanQueryInterface(handle, ctypes.byref(interface.guid), 7, None,
                                       ctypes.byref(size), ctypes.byref(attributes), None):
                continue
            try:
                profile = ctypes.wstring_at(attributes.value+8)
            finally:
                wlan.WlanFreeMemory(attributes)
            xml = ctypes.c_void_p()
            flags = ctypes.c_uint32(4)  # WLAN_PROFILE_GET_PLAINTEXT_KEY
            granted = ctypes.c_uint32()
            err = wlan.WlanGetProfile(handle, ctypes.byref(interface.guid), ctypes.c_wchar_p(profile), None,
                                      ctypes.byref(xml), ctypes.byref(flags), ctypes.byref(granted))
            if err:
                raise NetworkUnavailableError("当前网络配置/密码读取被系统拒绝，请手填一次")
            try:
                return parse_personal_wifi_profile(ctypes.wstring_at(xml.value))
            finally:
                wlan.WlanFreeMemory(xml)
        raise NetworkUnavailableError("没有已连接的 WLAN；请先连接个人 Wi-Fi，或使用 PC 热点")
    finally:
        if interfaces.value:
            wlan.WlanFreeMemory(interfaces)
        wlan.WlanCloseHandle(handle, None)

def _tethering_manager(profile_name=None):
    if sys.platform != "win32":
        raise NetworkUnavailableError("Windows hotspot API unavailable")
    try:
        from winrt.windows.networking.connectivity import NetworkInformation
        from winrt.windows.networking.networkoperators import NetworkOperatorTetheringManager
    except ImportError as error:
        raise NetworkUnavailableError("可选 WinRT 组件未安装；请使用“打开 Windows 热点设置”") from error
    try:
        profiles = [p for p in NetworkInformation.get_connection_profiles()
                    if p.is_wlan_connection_profile and int(p.get_network_connectivity_level()) > 0]
        if profile_name:
            profiles = [p for p in profiles if p.profile_name == profile_name]
        if len(profiles) != 1:
            raise NetworkUnavailableError("无法唯一确定共享上行 WLAN；请在 Windows 设置中选择")
        capability = NetworkOperatorTetheringManager.get_tethering_capability_from_connection_profile(profiles[0])
        if int(capability) != 0:
            raise NetworkUnavailableError(f"Windows 热点能力受限制（代码 {int(capability)}）；请使用系统设置")
        return NetworkOperatorTetheringManager.create_from_connection_profile(profiles[0])
    except NetworkUnavailableError:
        raise
    except Exception as error:
        raise NetworkUnavailableError("Windows 不允许当前程序管理热点；请使用系统设置") from error

def hotspot_status(profile_name=None) -> dict:
    try:
        manager = _tethering_manager(profile_name)
        state = manager.tethering_operational_state
        return {"available": True, "state": getattr(state, "name", str(state)),
                "clients": manager.client_count}
    except NetworkUnavailableError as error:
        return {"available": False, "reason": str(error)}

def hotspot_credentials(profile_name=None) -> dict:
    manager = _tethering_manager(profile_name)
    cfg = manager.get_current_access_point_configuration()
    return {"ssid": cfg.ssid, "password": cfg.passphrase}

def set_hotspot_enabled(enabled: bool, profile_name=None):
    manager = _tethering_manager(profile_name)
    async def change():
        result = await (manager.start_tethering_async() if enabled else manager.stop_tethering_async())
        if int(result.status) != 0:
            raise NetworkUnavailableError(f"热点操作失败（代码 {int(result.status)}）")
    asyncio.run(change())

def protect_bytes(data: bytes, *, decrypt=False) -> bytes:
    if sys.platform != "win32":
        raise NetworkUnavailableError("持久凭据需要 Windows 用户级保护存储")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    source_buffer = (ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    source = Blob(len(data), source_buffer)
    dest = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise NetworkUnavailableError("Windows 凭据保护操作失败")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(dest.data)
        ctypes.memset(source_buffer, 0, len(data))

class ProtectedSettings:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ADS1299P300" / "network.dat"

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        result = json.loads(protect_bytes(self.path.read_bytes(), decrypt=True))
        if not isinstance(result, dict):
            raise NetworkUnavailableError("受保护配置格式无效")
        return result

    def save(self, values: dict):
        encrypted = protect_bytes(json.dumps(values, ensure_ascii=False).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)
