// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_SIDE_PANEL_COORDINATOR_H_
#define CHROME_BROWSER_ENV_ENV_SIDE_PANEL_COORDINATOR_H_

#include <memory>

#include "base/memory/raw_ptr.h"
#include "base/memory/weak_ptr.h"
#include "ui/base/unowned_user_data/scoped_unowned_user_data.h"

class BrowserWindowInterface;
class SidePanelEntryScope;
class SidePanelRegistry;

namespace ui {
class UnownedUserDataHost;
}  // namespace ui

namespace views {
class View;
}  // namespace views

class Profile;

namespace env {

// Registers the environment switcher as a native-Views side panel entry and
// hands it the window it lives in.
//
// One coordinator per browser window: the side panel registry is window scoped,
// and switching an environment moves tabs within one window's tab strip, so a
// per-window object is the natural owner.
class EnvSidePanelCoordinator {
 public:
  EnvSidePanelCoordinator(ui::UnownedUserDataHost& unowned_user_data_host,
                          BrowserWindowInterface* browser,
                          SidePanelRegistry& global_registry);
  EnvSidePanelCoordinator(const EnvSidePanelCoordinator&) = delete;
  EnvSidePanelCoordinator& operator=(const EnvSidePanelCoordinator&) = delete;
  ~EnvSidePanelCoordinator();

  static EnvSidePanelCoordinator* From(BrowserWindowInterface* browser);

  // Shows the panel in every window of `profile`. One handler per profile is
  // not enough once each environment has its own window.
  static void ShowPanelEverywhere(Profile* profile);

  DECLARE_USER_DATA(EnvSidePanelCoordinator);

  // Opens the panel if closed, closes it if open. Called by the leading toolbar
  // button.
  void TogglePanel();

 private:
  std::unique_ptr<views::View> CreateEnvPanel(SidePanelEntryScope& scope);
  void ShowPanel();
  void ClosePanel();


  raw_ptr<BrowserWindowInterface> browser_;
  ui::ScopedUnownedUserData<EnvSidePanelCoordinator> scoped_unowned_user_data_;
  base::WeakPtrFactory<EnvSidePanelCoordinator> weak_factory_{this};
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_SIDE_PANEL_COORDINATOR_H_
