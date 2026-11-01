// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_MANAGER_H_
#define CHROME_BROWSER_ENV_ENV_MANAGER_H_

#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "base/functional/callback.h"
#include "base/memory/raw_ptr.h"
#include "base/memory/weak_ptr.h"
#include "components/tab_groups/tab_group_id.h"
#include "ui/gfx/geometry/rect.h"
#include "components/tab_groups/tab_group_visual_data.h"
#include "chrome/browser/env/automation_task.mojom.h"
#include "mojo/public/cpp/bindings/remote.h"
#include "base/observer_list.h"
#include "base/timer/timer.h"
#include "chrome/browser/env/env_profile.h"
#include "chrome/browser/ui/tabs/tab_strip_model_observer.h"
#include "content/public/browser/storage_partition_config.h"
#include "components/keyed_service/core/keyed_service.h"

class Profile;
namespace content {
class WebContents;
}

namespace env {

class AutomationServer;
class AutomationTaskImpl;

// Owns every browsing environment for a profile and performs the switching.
//
// Switching model: one window per environment. Only the active environment's
// window is visible; the others are hidden, so their pages keep loading and
// running while they are off screen.
// not destroy the WebContents -- measured: a detached page keeps running its
// timers at the original rate, so an environment that is not in front continues
// to work.
//
// One constraint shapes the order of operations: emptying the tab strip closes
// the window (TabStripEmpty). So new tabs are always inserted *before* the old
// ones are removed, never the other way round.
class EnvManager : public KeyedService, public TabStripModelObserver {
 public:
  // Told when the environment list or the active environment changes, so a UI
  // can redraw without polling the command channel or the filesystem.
  class Observer : public base::CheckedObserver {
   public:
    virtual void OnEnvsChanged() {}
    virtual void OnActiveEnvChanged(const std::string& env_id) {}
    // Tabs of the active environment changed (opened, closed, title, selection).
    virtual void OnTabsChanged() {}
  };

  // Returns the manager for `profile`, creating it on first use.
  static EnvManager* GetForProfile(Profile* profile);

  explicit EnvManager(Profile* profile);
  EnvManager(const EnvManager&) = delete;
  EnvManager& operator=(const EnvManager&) = delete;
  ~EnvManager() override;

  const std::vector<EnvProfile>& envs() const { return envs_; }
  const std::string& active_env_id() const { return active_env_id_; }

  // Human-readable environment name for a tab, or empty when the tab belongs to no
  // environment. Used for the window title (one window per environment).
  std::u16string DisplayNameForTab(content::WebContents* contents) const;
  bool window_per_env() const { return window_per_env_; }

  // Plan B: one window holds every environment's tabs and the tab strip is not
  // drawn at all, so switching is a tab activation with no window operation.
  bool hide_tab_strip() const { return hide_tab_strip_; }
  void set_hide_tab_strip(bool hide) { hide_tab_strip_ = hide; }

  // Closes (or opens) the environment side panel; used by the fingerprint
  // experiment to separate shell changes from Chrome itself.
  using PanelVisibilityHandler = base::RepeatingCallback<void(bool show)>;
  void SetPanelVisibilityHandler(PanelVisibilityHandler handler) {
    panel_visibility_handler_ = std::move(handler);
  }

  // One entry per tab of an environment, as the panel's tab bar shows it.
  struct TabInfo {
    int index = 0;
    std::u16string title;
    bool active = false;
  };
  std::vector<TabInfo> TabsForEnv(const std::string& env_id) const;
  bool ActivateTabInEnv(const std::string& env_id, int index);
  // Focus the active page so the tab does not keep its keyboard focus ring (posted: the
  // activation itself focuses the tab view).
  void FocusActivePage();
  bool CloseTabInEnv(const std::string& env_id, int index);

  const EnvProfile* FindEnv(const std::string& env_id) const;
  EnvProfile* FindMutableEnv(const std::string& env_id);

  // Creates or replaces an environment definition. Does not activate it.
  bool UpsertEnv(const EnvProfile& spec, std::string* error);
  // Removes only the definition. Persistent cookies and site data stay on disk.
  bool DeleteEnv(const std::string& env_id, std::string* error);

  // Makes `env_id` the active environment: inserts its tabs, then detaches the
  // tabs belonging to the previously active one. Returns false with a reason in
  // `error` when there is nothing to switch (no window, unknown environment).
  bool ActivateEnv(const std::string& env_id, std::string* error);
  // Populate the desktop launcher's first, still-hidden window with a real
  // environment tab. Never creates or shows another browser window.
  bool PrepareStartupWindow(BrowserWindowInterface* window);
  // One window per environment: keeps every env's tabs live so they can be
  // driven in parallel. Parked tabs have no CDP target (measured).
  bool ActivateEnvWindowPerEnv(const std::string& env_id,
                               std::string* error);

  // Opens one more tab in `env_id` at `url` (empty means about:blank). If the
  // environment is not the active one the tab is created and parked in its
  // detached set, so a caller can fill an environment before switching to it.
  bool NewTab(const std::string& env_id, const std::string& url,
              std::string* error);

  // Navigates an environment's existing tab (in the strip or parked) to `url`.
  // When the environment has no tab yet the URL becomes its start URL, so
  // "define then activate" works in one command. Backs `activate --url`.
  bool NavigateEnv(const std::string& env_id, const std::string& url,
                   std::string* error);

  // Resolves a StoragePartition path to the proxy rules of the environment that
  // owns it: "socks5://host:port", "http://host:port", or "direct". Returns an
  // empty string when the partition is not an environment, or the environment
  // has no proxy of its own -- callers should then leave the browser default
  // alone.
  std::string ProxyRulesForPartition(
      const content::StoragePartitionConfig& partition_config) const;

  // Which side-panel tab was last used ("envs" or "agents"), persisted with the
  // environment list so a restart lands where the user left off.
  const std::string& panel_tab() const { return panel_tab_; }
  void SetPanelTab(std::string tab);

  // Effective environment locale and Accept-Language. An unset environment uses
  // en-US,en, independently of the browser UI language.
  std::string LocaleForPartition(
      const content::StoragePartitionConfig& partition_config) const;
  std::string AcceptLanguagesForPartition(
      const content::StoragePartitionConfig& partition_config) const;
  std::string FingerprintLanguagesForPartition(
      const content::StoragePartitionConfig& partition_config) const;

  // Seed the renderer of `partition_config` should derive its identity from.
  // Empty when the partition is not an environment, which leaves the renderer
  // reporting the real identity.
  std::string SeedForPartition(
      const content::StoragePartitionConfig& partition_config) const;

  // Starts watching `strip` so that tabs the user opens (Ctrl+T, "open in new
  // tab", middle click) join the active environment instead of silently landing
  // in the default partition -- where they have no proxy of their own and leak the
  // machine's real address. Idempotent.
  void AttachToStrip(TabStripModel* strip);

  // TabStripModelObserver:
  void OnTabStripModelChanged(
      TabStripModel* tab_strip_model,
      const TabStripModelChange& change,
      const TabStripSelectionChange& selection) override;

  // Snapshot for the command channel / CLI, as JSON.
  base::ListValue LiveTabsJson(const std::string& env_id) const;
  content::WebContents* FindTabForEnv(const std::string& env_id) const;
  // Loopback socket for the automation bridge; only started when the browser
  // is launched with --lan-automation-port/--lan-automation-token.
  void MaybeStartAutomationServer();
  // Every environment window shares this rect, so switching looks like the page
  // changed in place instead of a window jumping.
  gfx::Rect per_env_bounds_;
  bool hide_tab_strip_ = false;
  // Which tab of each environment was selected when we left it, so switching back
  // returns to the same tab (the strip is hidden, so the user cannot re-pick it).
  std::map<std::string, content::WebContents*> remember_tab_;
  std::map<std::string, tab_groups::TabGroupId> env_groups_;
  std::unique_ptr<AutomationServer> automation_server_;
  mojo::Remote<mojom::AutomationTask> automation_task_;
  std::unique_ptr<AutomationTaskImpl> automation_task_impl_;
  std::string DescribeStateJson() const;
  std::string DescribeRuntimeJson() const;

  // Registers the callback used to bring the environment side panel on
  // screen. The manager owns the command channel, the UI layer owns the
  // panel, and this is the one line between them.
  // Opens the floating session window for `session_id` (title may be empty).
  using SessionRequestHandler =
      base::RepeatingCallback<void(std::string, std::string)>;
  void SetSessionRequestHandler(SessionRequestHandler handler) {
    session_request_handler_ = std::move(handler);
  }

  // The floating assistant window outlives side-panel rebuilds: the panel is torn
  // down whenever the side panel is toggled, and re-creating the window on the
  // next click crashed inside views::Widget::Init (traced).

  // Every environment lives in its own tab group inside the one strip. Switching
  // collapses the other groups instead of detaching tabs, so a background
  // environment keeps loading and running (operator requirement, §48).
  // Single-window model: one browser window holds every environment's tabs as background tabs,
  // and the tab strip is hidden so only the active environment is visible. Switching is a tab
  // activation, never a window operation.
  BrowserWindowInterface* EnsureMainWindow();
  // Read-only lookup: never creates a window, safe during window construction.
  BrowserWindowInterface* MainWindowIfReady() const;
  void HideTabStrip();
  // Extra windows (left over from the one-window-per-environment mode) are merged into the main
  // window, keeping their tabs.
  void MergeIntoMainWindow(BrowserWindowInterface* main_window);
  content::WebContents* TabForEnv(const std::string& env_id,
                                  TabStripModel* strip) const;
  bool ActivateEnvInWindow(const std::string& env_id, std::string* error);
  // Native tab groups: one per environment. Only the active environment's group is
  // expanded, so Chrome's own tab strip renders exactly its tabs.
  std::optional<tab_groups::TabGroupId> EnsureEnvGroup(
      const std::string& env_id,
      TabStripModel* strip);
  void ScheduleGroupSync();
  void SynchronizeEnvGroups();
  bool group_sync_pending_ = false;
  bool group_sync_in_progress_ = false;
  void SetGroupsCollapsedFor(const std::string& keep_env_id,
                             TabStripModel* strip);

  void SetPanelRequestHandler(base::RepeatingClosure handler) {
    panel_request_handler_ = std::move(handler);
  }

  void AddObserver(Observer* observer);
  void RemoveObserver(Observer* observer);

  raw_ptr<Profile> profile() const { return profile_; }

 private:
  friend class EnvManagerFactory;

  void LoadFromDisk();
  void SaveToDisk() const;

  // Polls `<profile>/env-command.json` for a pending command, executes it, and
  // answers in `<profile>/env-result.json`. Polling rather than a socket: a
  // switch is a human-scale action, and this avoids opening a port.
  void PollCommandFile();
  void HandleCommand(const std::string& json, std::string* result);

  // Moves tabs that landed in the default partition into the active
  // environment. Runs as a posted task: it replaces tabs, which cannot be done
  // from inside the insertion notification.
  void AdoptTabs();

  // Creates a WebContents bound to `spec`'s partition. Returns null on failure.
  std::unique_ptr<content::WebContents> CreateTabForEnv(const EnvProfile& spec);

  // Returns the environment id recorded on `contents`, or empty.
  static std::string EnvIdOf(content::WebContents* contents);


  raw_ptr<Profile> profile_;
  std::vector<EnvProfile> envs_;
  std::string active_env_id_;
  std::string panel_tab_ = "envs";
  // Default off: the existing gates and the single-window UX assume one window whose
  // strip is swapped. Turn it on (envs.json "window_per_env": true) for parallel work.
  // One window per environment is the product default: it keeps a background environment
  // running and keeps other environments out of the visible tab strip.
  bool window_per_env_ = true;


  base::RepeatingTimer command_timer_;
  base::ObserverList<Observer> observers_;
  base::RepeatingClosure panel_request_handler_;
  PanelVisibilityHandler panel_visibility_handler_;
  SessionRequestHandler session_request_handler_;
  raw_ptr<TabStripModel> observed_strip_;
  // The environment each pending tab should be adopted into, captured when the tab
  // was inserted: reading active_env_id_ later let a quick environment switch
  // relabel a page into the wrong environment.
  std::map<content::WebContents*, std::string> pending_adopt_env_;
  // Whether the pending tab was the selected one (its replacement must stay selected).
  std::set<content::WebContents*> pending_adopt_active_;
  base::WeakPtrFactory<EnvManager> weak_factory_{this};
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_MANAGER_H_
