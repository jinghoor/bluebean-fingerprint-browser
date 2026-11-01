#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#include <shellapi.h>

// GUI subsystem: configure only this installation's sandbox read access,
// without starting icacls, a console host, PowerShell or a command shell.
extern "C" void WINAPI WinMainCRTStartup() {
  int count = 0;
  LPWSTR* args = CommandLineToArgvW(GetCommandLineW(), &count);
  if (!args || count != 2) ExitProcess(ERROR_INVALID_PARAMETER);
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  PACL original = nullptr, updated = nullptr;
  DWORD result = GetNamedSecurityInfoW(args[1], SE_FILE_OBJECT,
      DACL_SECURITY_INFORMATION, nullptr, nullptr, &original, nullptr, &descriptor);
  PSID packages = nullptr, restricted = nullptr;
  if (result == ERROR_SUCCESS &&
      (!ConvertStringSidToSidW(L"S-1-15-2-1", &packages) ||
       !ConvertStringSidToSidW(L"S-1-15-2-2", &restricted)))
    result = GetLastError();
  if (result == ERROR_SUCCESS) {
    EXPLICIT_ACCESSW entries[2] = {};
    for (DWORD index = 0; index < 2; ++index) {
      entries[index].grfAccessPermissions = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE;
      entries[index].grfAccessMode = GRANT_ACCESS;
      entries[index].grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT;
      entries[index].Trustee.TrusteeForm = TRUSTEE_IS_SID;
      entries[index].Trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP;
      entries[index].Trustee.ptstrName = reinterpret_cast<LPWSTR>(
          index == 0 ? packages : restricted);
    }
    result = SetEntriesInAclW(2, entries, original, &updated);
    if (result == ERROR_SUCCESS)
      result = SetNamedSecurityInfoW(args[1], SE_FILE_OBJECT,
          DACL_SECURITY_INFORMATION, nullptr, nullptr, updated, nullptr);
  }
  if (updated) LocalFree(updated);
  if (descriptor) LocalFree(descriptor);
  if (packages) LocalFree(packages);
  if (restricted) LocalFree(restricted);
  LocalFree(args);
  ExitProcess(result);
}
