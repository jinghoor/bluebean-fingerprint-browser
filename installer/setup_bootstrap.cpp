#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <shlobj.h>
#include <sddl.h>
#include <bcrypt.h>

// An ordinary GUI entry point: select a private writable temporary directory
// before Inno Setup's loader runs. No machine or user settings are modified.
struct PayloadFooter {
  char magic[16];
  ULONGLONG offset;
  ULONGLONG size;
  BYTE sha256[32];
};
static wchar_t self_path[32768], base_path[32768], work_path[32768];
static wchar_t setup_path[32768], command[32768], descriptor[512];
static BYTE buffer[1024 * 1024], token_buffer[4096];
static const char payload_magic[] = "BBSETUP_PAYLOAD1";
static PayloadFooter footer;
static SECURITY_ATTRIBUTES security;
static STARTUPINFOW startup;
static PROCESS_INFORMATION child;
static bool silent = false;

static void Error(const wchar_t* message) {
  if (!silent) MessageBoxW(nullptr, message, L"蓝豆指纹浏览器安装", MB_OK | MB_ICONERROR);
}

static bool HasNormalIntegrity() {
  HANDLE token;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
  DWORD bytes = 0;
  BOOL ok = GetTokenInformation(token, TokenIntegrityLevel, token_buffer,
                               sizeof(token_buffer), &bytes);
  CloseHandle(token);
  if (!ok) return false;
  PSID sid = reinterpret_cast<TOKEN_MANDATORY_LABEL*>(token_buffer)->Label.Sid;
  DWORD level = *GetSidSubAuthority(sid, *GetSidSubAuthorityCount(sid) - 1);
  return level >= SECURITY_MANDATORY_MEDIUM_RID;
}

static bool Append(wchar_t* target, const wchar_t* value) {
  if (lstrlenW(target) + lstrlenW(value) >= 32767) return false;
  lstrcatW(target, value);
  return true;
}

static bool PrivateSecurity() {
  HANDLE token;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
  DWORD bytes = 0;
  BOOL ok = GetTokenInformation(token, TokenUser, token_buffer,
                               sizeof(token_buffer), &bytes);
  CloseHandle(token);
  if (!ok) return false;
  LPWSTR sid = nullptr;
  if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(token_buffer)->User.Sid,
                             &sid)) return false;
  lstrcpyW(descriptor, L"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;");
  lstrcatW(descriptor, sid);
  lstrcatW(descriptor, L")");
  LocalFree(sid);
  security.nLength = sizeof(security);
  return ConvertStringSecurityDescriptorToSecurityDescriptorW(
      descriptor, SDDL_REVISION_1, &security.lpSecurityDescriptor, nullptr) != FALSE;
}

static bool TryBase(const wchar_t* base) {
  if (!base || !*base || lstrlenW(base) > 32000) return false;
  DWORD attributes = GetFileAttributesW(base);
  if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY))
    return false;
  BYTE random[16];
  if (BCryptGenRandom(nullptr, random, sizeof(random),
                      BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) return false;
  static const wchar_t digits[] = L"0123456789abcdef";
  lstrcpyW(work_path, base);
  Append(work_path, L"\\BlueBeanSetup-");
  DWORD index = lstrlenW(work_path);
  for (DWORD n = 0; n < sizeof(random); ++n) {
    work_path[index++] = digits[random[n] >> 4];
    work_path[index++] = digits[random[n] & 15];
  }
  work_path[index] = 0;
  if (!CreateDirectoryW(work_path, &security)) return false;
  lstrcpyW(setup_path, work_path);
  Append(setup_path, L"\\BlueBean-Install.exe");
  HANDLE file = CreateFileW(setup_path, GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                            FILE_ATTRIBUTE_TEMPORARY, nullptr);
  if (file == INVALID_HANDLE_VALUE) {
    RemoveDirectoryW(work_path);
    return false;
  }
  CloseHandle(file);
  return true;
}

static bool SelectTemp() {
  if (!PrivateSecurity()) return false;
  // The Windows known folder is independent of inherited TEMP/TMP values.
  if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_LOCAL_APPDATA, nullptr,
                                 SHGFP_TYPE_CURRENT, base_path)) && TryBase(base_path))
    return true;
  DWORD length = GetTempPathW(32768, base_path);
  if (length && length < 32768 && TryBase(base_path)) return true;
  // A downloaded installer may be in a writable folder while TEMP is broken.
  lstrcpyW(base_path, self_path);
  DWORD n = lstrlenW(base_path);
  while (n && base_path[n - 1] != L'\\') --n;
  if (!n) return false;
  base_path[n - 1] = 0;
  return TryBase(base_path);
}

static bool Unpack() {
  HANDLE input = CreateFileW(self_path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (input == INVALID_HANDLE_VALUE) return false;
  LARGE_INTEGER length, position;
  DWORD read = 0;
  bool ok = GetFileSizeEx(input, &length) && length.QuadPart > sizeof(footer);
  if (ok) {
    position.QuadPart = length.QuadPart - sizeof(footer);
    ok = SetFilePointerEx(input, position, nullptr, FILE_BEGIN) &&
         ReadFile(input, &footer, sizeof(footer), &read, nullptr) && read == sizeof(footer);
  }
  if (ok) {
    for (DWORD n = 0; n < sizeof(footer.magic); ++n)
      if (footer.magic[n] != payload_magic[n]) ok = false;
    if (footer.offset < 512 || footer.size < 1024 * 1024 ||
        footer.offset > static_cast<ULONGLONG>(length.QuadPart) - sizeof(footer) ||
        footer.size != static_cast<ULONGLONG>(length.QuadPart) - sizeof(footer) - footer.offset)
      ok = false;
  }
  HANDLE output = INVALID_HANDLE_VALUE;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  PBYTE hash_object = nullptr;
  DWORD object_size = 0, property_size = 0;
  if (ok) {
    ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0 &&
         BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                            reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
                            &property_size, 0) >= 0;
  }
  if (ok) {
    hash_object = static_cast<PBYTE>(HeapAlloc(GetProcessHeap(), 0, object_size));
    ok = hash_object && BCryptCreateHash(algorithm, &hash, hash_object, object_size,
                                         nullptr, 0, 0) >= 0;
  }
  if (ok) {
    output = CreateFileW(setup_path, GENERIC_WRITE, 0, nullptr, TRUNCATE_EXISTING,
                          FILE_ATTRIBUTE_NORMAL, nullptr);
    position.QuadPart = footer.offset;
    ok = output != INVALID_HANDLE_VALUE && SetFilePointerEx(input, position, nullptr, FILE_BEGIN);
  }
  ULONGLONG remaining = footer.size;
  while (ok && remaining) {
    DWORD chunk = remaining > sizeof(buffer) ? sizeof(buffer) : static_cast<DWORD>(remaining);
    DWORD written = 0;
    ok = ReadFile(input, buffer, chunk, &read, nullptr) && read == chunk &&
         BCryptHashData(hash, buffer, read, 0) >= 0 &&
         WriteFile(output, buffer, read, &written, nullptr) && written == read;
    remaining -= read;
  }
  BYTE digest[32];
  if (ok) {
    ok = BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0;
    for (DWORD n = 0; n < sizeof(digest); ++n)
      if (digest[n] != footer.sha256[n]) ok = false;
  }
  if (hash) BCryptDestroyHash(hash);
  if (hash_object) HeapFree(GetProcessHeap(), 0, hash_object);
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  if (output != INVALID_HANDLE_VALUE) CloseHandle(output);
  CloseHandle(input);
  return ok;
}

extern "C" void WINAPI WinMainCRTStartup() {
  int argument_count = 0;
  LPWSTR* arguments = CommandLineToArgvW(GetCommandLineW(), &argument_count);
  if (arguments) {
    for (int i = 1; i < argument_count; ++i)
      if (!lstrcmpiW(arguments[i], L"/VERYSILENT") || !lstrcmpiW(arguments[i], L"/SILENT"))
        silent = true;
    LocalFree(arguments);
  }
  DWORD n = GetModuleFileNameW(nullptr, self_path, 32768);
  if (!n || n >= 32768) ExitProcess(1);
  if (!HasNormalIntegrity()) {
    Error(L"Windows 将此安装程序限制为低权限，无法写入正常安装目录。\n请重新下载最新版安装包到“下载”文件夹后运行。无需修改系统文件夹权限。");
    ExitProcess(13);
  }
  if (!SelectTemp()) {
    Error(L"无法创建安全的安装临时目录。请将安装包复制到你可以写入的本地文件夹后重试。\n系统临时目录的权限不会被修改。");
    ExitProcess(10);
  }
  if (!Unpack()) {
    DeleteFileW(setup_path);
    RemoveDirectoryW(work_path);
    Error(L"安装包校验或解压失败，请重新下载安装包，并检查磁盘可用空间。");
    ExitProcess(11);
  }
  LocalFree(security.lpSecurityDescriptor);
  SetEnvironmentVariableW(L"TEMP", work_path);
  SetEnvironmentVariableW(L"TMP", work_path);
  // Keep the original installer arguments byte-for-byte. Windows does not use
  // a command shell here; the application path is supplied explicitly.
  const wchar_t* tail = GetCommandLineW();
  if (*tail == L'"') {
    ++tail;
    while (*tail && *tail != L'"') ++tail;
    if (*tail) ++tail;
  } else {
    while (*tail && *tail != L' ' && *tail != L'\t') ++tail;
  }
  lstrcpyW(command, L"\"");
  bool ok = Append(command, setup_path) && Append(command, L"\" ") && Append(command, tail);
  startup.cb = sizeof(startup);
  DWORD result = 12;
  if (ok && CreateProcessW(setup_path, command, nullptr, nullptr, FALSE, 0,
                           nullptr, work_path, &startup, &child)) {
    WaitForSingleObject(child.hProcess, INFINITE);
    GetExitCodeProcess(child.hProcess, &result);
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
  } else {
    Error(L"无法启动安装向导，请检查安全软件的拦截记录。");
  }
  DeleteFileW(setup_path);
  RemoveDirectoryW(work_path);
  ExitProcess(result);
}
