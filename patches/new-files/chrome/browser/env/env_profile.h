// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_PROFILE_H_
#define CHROME_BROWSER_ENV_ENV_PROFILE_H_

#include <string>

#include "base/files/file_path.h"

namespace env {

// One isolated browsing environment: a StoragePartition plus the settings that
// make it look like a distinct person and exit through a distinct address.
//
// `partition_name` is the identity that survives restarts -- everything the site
// stores lives under it. The other fields are inputs to the proxy and
// fingerprint layers and are only meaningful while that partition is active.
struct EnvProfile {
  // Stable identifier, used in the command file and as the partition name.
  // Must be unique and file-system safe (it becomes a directory name).
  std::string env_id;

  // Shown in the side panel. Falls back to env_id when empty.
  std::string display_name;

  // Local organization metadata shown by the environment manager.
  std::string group;
  std::string tags;
  std::string notes;

  // Proxy for this environment: "socks5://host:port", "http://host:port",
  // "direct", or "system". Empty means "leave the browser default alone".
  std::string proxy_config;

  // Page opened in the environment's first tab. Empty means about:blank.
  // Must not be a chrome:// URL: navigating into WebUI replaces the
  // BrowsingInstance and drops the fixed StoragePartition (measured in M1).
  std::string start_url;

  // Page a new tab opens (the strip's + and `newtab` without a URL). Empty means
  // kDefaultHomepage. Separate from start_url: the first tab may be specific while every
  // later tab should still land on the home page.
  std::string homepage;

  // Seed for the fingerprint derivation. Empty means "derive from env_id".
  std::string fingerprint_seed;

  // Language the environment presents: `locale` drives ICU in the renderer
  // (date/number formatting, Intl's default locale) and `accept_languages` is the
  // header value sent with every request. The first Accept-Language tag is the
  // canonical language when both are set. If only one is set, it supplies both
  // values; if neither is set, environments default to en-US,en independently
  // of the browser UI language.
  std::string locale;
  std::string accept_languages;

  // Where this environment is expected to exit. Recorded so a moved exit can be
  // reported instead of silently accepted -- session-sticky proxies expire.
  std::string expected_exit_ip;

  // Static exits require confirmation when they change. Dynamic exits are still
  // checked for connectivity, but changing addresses do not require confirmation.
  std::string ip_mode = "static";

  // Non-empty once the environment has been activated at least once.
  bool used = false;

  // The partition name handed to StoragePartitionConfig. Chromium requires a
  // non-empty domain, so this is paired with a fixed domain in env_manager.cc.
  std::string PartitionName() const;
  std::string ResolvedSeed() const;
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_PROFILE_H_
