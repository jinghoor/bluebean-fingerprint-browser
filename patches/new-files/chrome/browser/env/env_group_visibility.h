// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_GROUP_VISIBILITY_H_
#define CHROME_BROWSER_ENV_ENV_GROUP_VISIBILITY_H_

#include "components/tab_groups/tab_group_id.h"
#include "base/functional/callback.h"
#include "base/values.h"

namespace env {

// While the environment mode is on, a collapsed tab group belongs to an environment that is not on
// screen, so the strip must not draw its header (the user should see only the active environment's
// tabs, drawn by Chrome itself).
void SetHideCollapsedGroupHeaders(bool hide);

// Groups created for environments: their headers are never drawn, so the strip looks like
// a plain Chrome tab strip holding only the active environment's tabs.
void RegisterEnvGroup(const tab_groups::TabGroupId& group);
bool IsEnvGroup(const tab_groups::TabGroupId& group);
bool HideCollapsedGroupHeaders();
// The diagnostics command pulls a live snapshot: a cached copy can lag behind the model because
// selecting a tab does not always trigger another layout.
using GeometryProvider = base::RepeatingCallback<base::DictValue()>;
// One provider per TabContainer: the startup window and the main window both exist, and only the one
// holding environment groups can answer.
void RegisterGeometryProvider(const void* owner, GeometryProvider provider);
void UnregisterGeometryProvider(const void* owner);
base::DictValue GetTabStripGeometry();

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_GROUP_VISIBILITY_H_
