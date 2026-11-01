// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_TAB_BAR_H_
#define CHROME_BROWSER_ENV_ENV_TAB_BAR_H_

#include <string>
#include <vector>

#include "base/memory/raw_ptr.h"
#include "base/memory/weak_ptr.h"
#include "chrome/browser/env/env_manager.h"
#include "ui/base/metadata/metadata_header_macros.h"
#include "ui/views/view.h"

namespace env {

// Horizontal tab bar for the active environment, drawn into the window's caption band.
//
// Chrome's own tab strip is hidden (design doc §52), so this is the only place tabs are visible.
// It draws one chip per tab of the active environment plus a "+", and drives the tab model through
// `EnvManager` only -- it never touches TabStripModel or windows.
class EnvTabBar : public views::View, public EnvManager::Observer {
  METADATA_HEADER(EnvTabBar, views::View)

 public:
  explicit EnvTabBar(EnvManager* manager);
  EnvTabBar(const EnvTabBar&) = delete;
  EnvTabBar& operator=(const EnvTabBar&) = delete;
  ~EnvTabBar() override;

  // Rebuilds the chips from the tab model.
  void Refresh();

  // views::View:
  void Layout(PassKey) override;
  void OnThemeChanged() override;

  // EnvManager::Observer:
  void OnEnvsChanged() override;
  void OnActiveEnvChanged(const std::string& env_id) override;
  void OnTabsChanged() override;

 private:
  raw_ptr<EnvManager> manager_;
  base::WeakPtrFactory<EnvTabBar> weak_factory_{this};
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_TAB_BAR_H_
