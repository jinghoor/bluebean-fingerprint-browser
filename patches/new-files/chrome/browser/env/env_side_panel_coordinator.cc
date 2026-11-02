// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_side_panel_coordinator.h"

#include <memory>
#include <utility>

#include "base/command_line.h"
#include "base/functional/bind.h"
#include "base/functional/callback_helpers.h"
#include "base/location.h"
#include "base/logging.h"
#include "base/memory/raw_ptr.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "chrome/browser/env/env_manager.h"
#include "chrome/browser/env/env_sidebar_panel_view.h"
#include "chrome/browser/env/env_overlay.h"
#include "chrome/browser/ui/browser_window/public/profile_browser_collection.h"
#include "chrome/browser/ui/browser_window/public/browser_collection.h"
#include "chrome/browser/profiles/profile.h"
#include "chrome/browser/ui/browser_window/public/browser_window_interface.h"
#include "chrome/browser/ui/side_panel/side_panel_entry.h"
#include "chrome/browser/ui/side_panel/side_panel_entry_id.h"
#include "chrome/browser/ui/side_panel/side_panel_enums.h"
#include "chrome/browser/ui/side_panel/side_panel_registry.h"
#include "chrome/browser/ui/side_panel/side_panel_ui.h"
#include "base/values.h"
#include "chrome/common/pref_names.h"
#include "components/prefs/pref_service.h"
#include "ui/base/unowned_user_data/unowned_user_data_host.h"
#include "ui/views/view.h"

namespace env {


DEFINE_USER_DATA(EnvSidePanelCoordinator);

// static
EnvSidePanelCoordinator* EnvSidePanelCoordinator::From(
    BrowserWindowInterface* browser) {
  return browser ? EnvSidePanelCoordinator::Get(
                       browser->GetUnownedUserDataHost())
                 : nullptr;
}

EnvSidePanelCoordinator::EnvSidePanelCoordinator(
    ui::UnownedUserDataHost& unowned_user_data_host,
    BrowserWindowInterface* browser,
    SidePanelRegistry& global_registry)
    : browser_(browser),
      scoped_unowned_user_data_(unowned_user_data_host, *this) {
  // Window scoped registry: the environment list belongs to the window whose tab
  // strip is being switched, not to a tab. Registering on the tab-scoped
  // registry would tie the panel's lifetime to whichever tab happened to be
  // current, and switching environments replaces exactly that set of tabs.
  // Docked sidebar disabled by product decision: Chrome's side panel is ~282 px and DSH folds its sidebar into an
  // icon rail below ~1000 CSS px, so a docked copy could only ever show icons next to the real sidebar in the layer.
  // Nothing is registered here, which also means no empty side strip can appear.
  LOG(ERROR) << "ENVPANEL: docked sidebar disabled; the layer carries the sidebar";

  // Let the command channel (and the CLI) bring the panel on screen.
  // Showing it from the window's construction instead left the window with
  // a panel and no tab at all -- measured twice, once from the features
  // constructor and once 300ms after the first tab.
  if (Profile* profile = browser ? browser->GetProfile() : nullptr) {
    EnvManager* manager = EnvManager::GetForProfile(profile);
    manager->SetPanelRequestHandler(base::BindRepeating(
        &EnvSidePanelCoordinator::ShowPanelEverywhere, profile));
    manager->SetPanelVisibilityHandler(base::BindRepeating(
        [](Profile* profile, bool show) {
          ProfileBrowserCollection* collection =
              ProfileBrowserCollection::GetForProfile(profile);
          if (!collection) {
            return;
          }
          collection->ForEach(
              [show](BrowserWindowInterface* window) {
                if (EnvSidePanelCoordinator* coordinator =
                        EnvSidePanelCoordinator::From(window)) {
                  if (show) {
                    coordinator->ShowPanel();
                  } else {
                    coordinator->ClosePanel();
                  }
                }
                return true;
              },
              BrowserCollection::Order::kCreation);
        },
        profile));
  }
}

EnvSidePanelCoordinator::~EnvSidePanelCoordinator() = default;

// static
void EnvSidePanelCoordinator::ShowPanelEverywhere(Profile* profile) {
  if (!profile) {
    return;
  }
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile);
  if (!collection) {
    return;
  }
  int shown = 0;
  collection->ForEach(
      [&shown](BrowserWindowInterface* window) {
        if (EnvSidePanelCoordinator* coordinator =
                EnvSidePanelCoordinator::From(window)) {
          coordinator->ShowPanel();
          ++shown;
        }
        return true;
      },
      BrowserCollection::Order::kCreation);
  LOG(ERROR) << "ENVPANEL: panel request fanned out to " << shown << " window(s)";
}

void EnvSidePanelCoordinator::ClosePanel() {
  if (!browser_) {
    return;
  }
  if (SidePanelUI* ui = SidePanelUI::From(browser_)) {
    ui->Close();
    LOG(ERROR) << "ENVPANEL: closed the side panel";
  }
  env::HideOverlay(browser_);
}

void EnvSidePanelCoordinator::ShowPanel() {
  if (!browser_ || browser_->GetType() != BrowserWindowInterface::TYPE_NORMAL) {
    return;
  }
  // Docking the workbench here is not viable: Chrome's side panel is ~282 px and DSH folds its sidebar into an icon
  // rail below ~1000 CSS px, so the docked copy showed icons only while the layer kept the real sidebar. The layer
  // carries the sidebar; showing must be idempotent (toggling hid a visible layer on every environment switch).
  LOG(ERROR) << "ENVPANEL: showing the assistant layer (docking is not viable at this width)";
  env::ShowAssistantLayer(browser_);
}

void EnvSidePanelCoordinator::TogglePanel() {
  if (!browser_ || browser_->GetType() != BrowserWindowInterface::TYPE_NORMAL) {
    return;
  }
  LOG(ERROR) << "ENVPANEL: toggling the assistant layer (docking is not viable at this width)";
  env::ToggleAssistantLayer(browser_);
}

std::unique_ptr<views::View> EnvSidePanelCoordinator::CreateEnvPanel(
    SidePanelEntryScope& scope) {
  Profile* profile = browser_ ? browser_->GetProfile() : nullptr;
  EnvManager* manager = profile ? EnvManager::GetForProfile(profile) : nullptr;
  if (!manager) {
    LOG(ERROR) << "ENVPANEL: no environment manager for this window";
  }
  // Nothing shows this entry any more (docking was reverted); the guard keeps an accidental show from rendering the
  // workbench at a width where DSH only draws its icon rail.
  LOG(ERROR) << "ENVPANEL: side panel content requested while docking is disabled";
  return std::make_unique<views::View>();
}

}  // namespace env
