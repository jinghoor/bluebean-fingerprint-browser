// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include <map>
#include <set>
#include "base/no_destructor.h"
#include "chrome/browser/env/env_group_visibility.h"

namespace env {
namespace {
bool g_hide_collapsed_group_headers = true;
int g_pull_sequence = 0;

std::map<const void*, GeometryProvider>& GeometryProviders() {
  static base::NoDestructor<std::map<const void*, GeometryProvider>> providers;
  return *providers;
}
std::set<tab_groups::TabGroupId>& EnvGroups() {
  static base::NoDestructor<std::set<tab_groups::TabGroupId>> groups;
  return *groups;
}
}  // namespace

void SetHideCollapsedGroupHeaders(bool hide) {
  g_hide_collapsed_group_headers = hide;
}

bool HideCollapsedGroupHeaders() {
  return g_hide_collapsed_group_headers;
}

void RegisterEnvGroup(const tab_groups::TabGroupId& group) {
  EnvGroups().insert(group);
}

bool IsEnvGroup(const tab_groups::TabGroupId& group) {
  return EnvGroups().count(group) > 0;
}

void RegisterGeometryProvider(const void* owner, GeometryProvider provider) {
  GeometryProviders()[owner] = std::move(provider);
}

void UnregisterGeometryProvider(const void* owner) {
  GeometryProviders().erase(owner);
}

base::DictValue GetTabStripGeometry() {
  for (auto& [owner, provider] : GeometryProviders()) {
    base::DictValue snapshot = provider.is_null() ? base::DictValue() : provider.Run();
    if (!snapshot.empty()) {
      snapshot.Set("sequence", ++g_pull_sequence);
      return snapshot;
    }
  }
  return base::DictValue();
}

}  // namespace env
