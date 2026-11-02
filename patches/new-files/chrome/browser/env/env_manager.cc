// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_group_visibility.h"
#include "chrome/browser/env/env_manager.h"

#include "chrome/browser/env/env_overlay.h"
#include "chrome/browser/env/automation_task_impl.h"
#include "components/tab_groups/tab_group_visual_data.h"
#include "chrome/browser/env/automation_server.h"
#include "base/strings/string_number_conversions.h"
#include "base/command_line.h"
#include "base/unguessable_token.h"
#include "base/auto_reset.h"
#include "chrome/browser/ui/tabs/tab_group_model.h"

#include <set>
#include <algorithm>
#include <string_view>
#include <utility>

#include "base/files/file_util.h"
#include "base/json/json_reader.h"
#include "base/json/json_writer.h"
#include "base/logging.h"
#include "base/no_destructor.h"
#include "base/strings/string_util.h"
#include "base/strings/stringprintf.h"
#include "base/strings/utf_string_conversions.h"
#include "base/task/sequenced_task_runner.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "base/values.h"
#include "chrome/browser/env/env_profile.h"
#include "chrome/browser/profiles/profile.h"
#include "chrome/browser/ui/browser_window/public/create_browser_window.h"
#include "chrome/browser/ui/browser_window/public/browser_collection.h"
#include "chrome/browser/ui/browser_window/public/browser_window_interface.h"
#include "chrome/browser/ui/browser_window/public/profile_browser_collection.h"
#include "chrome/browser/ui/tabs/tab_enums.h"
#include "chrome/browser/ui/tabs/tab_strip_model.h"
#include "chrome/browser/ui/tabs/tab_strip_model_observer.h"
#include "ui/base/base_window.h"
#include "ui/display/display.h"
#include "ui/display/screen.h"
#include "ui/views/win/hwnd_util.h"
#include "content/public/browser/navigation_controller.h"
#include "content/public/browser/navigation_handle.h"
#include "content/public/browser/navigation_throttle.h"
#include "content/public/browser/navigation_throttle_registry.h"
#include "ui/views/focus/focus_manager.h"
#include "ui/views/widget/widget.h"
#include "content/public/browser/security_principal.h"
#include "content/public/browser/site_instance.h"
#include "content/public/browser/storage_partition_config.h"
#include "content/public/browser/devtools_agent_host.h"
#include "base/run_loop.h"
#include "content/public/browser/render_frame_host.h"
#include "content/public/browser/web_contents.h"
#include "content/public/common/url_constants.h"
#include "url/gurl.h"
#include "url/url_constants.h"

namespace env {
namespace {
// Product default for a new tab: the operator's current home page.
constexpr char kDefaultHomepage[] = "https://www.google.com";


// StoragePartitionConfig::Create requires a non-empty [a-z]* partition domain.
// Using a fixed domain and putting the environment id in the partition name
// keeps all environments in one namespace, which is what the on-disk layout and
// the fallback-for-blob rules expect.
constexpr char kPartitionDomain[] = "env";

// Prefix of every environment's partition name. One place so the create path and
// both lookups (proxy, seed) cannot drift apart.
constexpr std::string_view kPartitionNamePrefix = "env-";

constexpr char kCommandFileName[] = "env-command.json";
constexpr char kResultFileName[] = "env-result.json";
constexpr char kStateFileName[] = "envs.json";

// A switch is a human action; polling this often is imperceptible and avoids
// opening a socket just to accept commands.
constexpr base::TimeDelta kCommandPollInterval = base::Milliseconds(250);

class StartupCheckThrottle : public content::NavigationThrottle {
 public:
  explicit StartupCheckThrottle(content::NavigationThrottleRegistry& registry)
      : content::NavigationThrottle(registry) {}
  const char* GetNameForLogging() override { return "EnvironmentStartupCheck"; }
  ThrottleCheckResult WillStartRequest() override { return Check(); }
  ThrottleCheckResult WillRedirectRequest() override { return Check(); }
  ThrottleCheckResult WillCommitWithoutUrlLoader() override { return Check(); }
 private:
  ThrottleCheckResult Check() {
    auto* handle = navigation_handle();
    auto* profile = Profile::FromBrowserContext(handle->GetWebContents()->GetBrowserContext());
    return EnvManager::GetForProfile(profile)->BlockStartupNavigation(handle)
               ? CANCEL_AND_IGNORE : PROCEED;
  }
};




}  // namespace

// ---------------------------------------------------------------------------
// EnvProfile
// ---------------------------------------------------------------------------

std::string EnvProfile::PartitionName() const {
  return std::string(kPartitionNamePrefix) + env_id;
}

std::string EnvProfile::ResolvedSeed() const {
  return fingerprint_seed.empty() ? ("env:" + env_id) : fingerprint_seed;
}

// ---------------------------------------------------------------------------
// EnvManager
// ---------------------------------------------------------------------------

// static
EnvManager* EnvManager::GetForProfile(Profile* profile) {
  static base::NoDestructor<std::map<Profile*, std::unique_ptr<EnvManager>>>
      managers;
  auto it = managers->find(profile);
  if (it != managers->end()) {
    return it->second.get();
  }
  auto manager = std::make_unique<EnvManager>(profile);
  EnvManager* raw = manager.get();
  (*managers)[profile] = std::move(manager);
  return raw;
}

EnvManager::EnvManager(Profile* profile) : profile_(profile) {
  LoadFromDisk();
  command_timer_.Start(FROM_HERE, kCommandPollInterval, this,
                       &EnvManager::PollCommandFile);
  MaybeStartAutomationServer();
}

EnvManager::~EnvManager() = default;

void EnvManager::AddObserver(Observer* observer) {
  observers_.AddObserver(observer);
}

void EnvManager::RemoveObserver(Observer* observer) {
  observers_.RemoveObserver(observer);
}

const EnvProfile* EnvManager::FindEnv(const std::string& env_id) const {
  for (const EnvProfile& e : envs_) {
    if (e.env_id == env_id) {
      return &e;
    }
  }
  return nullptr;
}

EnvProfile* EnvManager::FindMutableEnv(const std::string& env_id) {
  for (EnvProfile& e : envs_) {
    if (e.env_id == env_id) {
      return &e;
    }
  }
  return nullptr;
}

bool EnvManager::UpsertEnv(const EnvProfile& spec, std::string* error) {
  if (spec.env_id.empty()) {
    *error = "env_id is required";
    return false;
  }
  if (EnvProfile* existing = FindMutableEnv(spec.env_id)) {
    if (existing->proxy_config != spec.proxy_config && startup_gates_.contains(spec.env_id)) {
      // The existing NetworkContext retains its old proxy until browser restart.
      // Never validate the new proxy and then release pages using the old one.
      startup_gates_.at(spec.env_id).unlocked = false;
    } else if (existing->expected_exit_ip != spec.expected_exit_ip ||
        existing->ip_mode != spec.ip_mode) {
      startup_gates_.erase(spec.env_id);
    }
    *existing = spec;
  } else {
    envs_.push_back(spec);
  }
  SaveToDisk();
  observers_.Notify(&Observer::OnEnvsChanged);
  return true;
}

bool EnvManager::DeleteEnv(const std::string& env_id, std::string* error) {
  if (env_id.empty()) {
    *error = "env_id is required";
    return false;
  }
  if (env_id == active_env_id_) {
    *error = "switch to another environment before removing this one";
    return false;
  }
  if (FindTabForEnv(env_id)) {
    *error = "close this environment's browser tabs before removing its definition";
    return false;
  }
  auto it = std::find_if(envs_.begin(), envs_.end(),
                         [&env_id](const EnvProfile& item) {
                           return item.env_id == env_id;
                         });
  if (it == envs_.end()) {
    *error = "unknown environment: " + env_id;
    return false;
  }
  envs_.erase(it);
  SaveToDisk();
  observers_.Notify(&Observer::OnEnvsChanged);
  return true;
}

// static
std::string EnvManager::EnvIdOf(content::WebContents* contents) {
  if (!contents) {
    return std::string();
  }
  // Ownership comes from the tab's own partition, not from a tag attached to it.
  // Measured reason: tabs that were parked and later put back reported an empty
  // tag ("keeping a just-inserted tab whose env id is empty"), which made a switch
  // detach the tab it had just inserted and empty the strip. The partition cannot
  // desynchronise from the tab: it is what the tab was created with.
  content::SiteInstance* site_instance = contents->GetSiteInstance();
  if (!site_instance) {
    return std::string();
  }
  const content::StoragePartitionConfig& partition =
      site_instance->GetSecurityPrincipal().GetStoragePartitionConfig();
  if (partition.is_default()) {
    return std::string();
  }
  const std::string& name = partition.partition_name();
  if (!base::StartsWith(name, kPartitionNamePrefix)) {
    return std::string();
  }
  return name.substr(kPartitionNamePrefix.size());
}

std::unique_ptr<content::WebContents> EnvManager::CreateTabForEnv(
    const EnvProfile& spec) {
  // A non-default partition is required by CreateForFixedStoragePartition; an
  // empty domain would be interpreted as "this is the default partition" and
  // conflicts with a non-empty name (that mismatch trips a CHECK).
  content::StoragePartitionConfig partition =
      content::StoragePartitionConfig::Create(
          profile_, kPartitionDomain, spec.PartitionName(),
          /*in_memory=*/false);

  // The URL only seeds the SiteInstance's site; the tab starts blank and the
  // user navigates. Passing about:blank keeps the initial state neutral.
  scoped_refptr<content::SiteInstance> site_instance =
      content::SiteInstance::CreateForFixedStoragePartition(
          profile_, GURL(url::kAboutBlankURL), partition);
  if (!site_instance) {
    LOG(ERROR) << "ENVMGR: CreateForFixedStoragePartition failed for "
               << spec.env_id;
    return nullptr;
  }

  content::WebContents::CreateParams params(profile_, std::move(site_instance));
  std::unique_ptr<content::WebContents> contents =
      content::WebContents::Create(params);
  if (!contents) {
    LOG(ERROR) << "ENVMGR: WebContents::Create failed for " << spec.env_id;
    return nullptr;
  }
  VLOG(1) << "ENVMGR: created tab ptr=" << contents.get()
          << " env=" << spec.env_id
          << " partition=" << partition.partition_name();

  // WebContents::Create only builds the skeleton: no navigation has happened, so
  // the CDP target reports an empty URL and the renderer never comes up, which
  // makes the tab unaddressable by automation. Navigating turns it into a real
  // tab.
  // about:blank rather than chrome://newtab: navigating into a WebUI replaces
  // the BrowsingInstance, and with it the fixed StoragePartition this tab was
  // created with -- the tab then silently falls back to the default partition.
  // about:blank has an empty site, so it initialises the renderer (CDP can
  // attach, automation can drive it) without leaving the fixed partition. An
  // http(s) start_url is safe for a different reason: the tab was created with
  // the partition already fixed, so a real navigation inherits it.
  GURL start(url::kAboutBlankURL);
  EnsureStartupGate(spec);
  if (!spec.start_url.empty()) {
    GURL candidate(spec.start_url);
    // chrome:// (WebUI) is rejected here rather than at navigation time: see
    // above -- it replaces the BrowsingInstance and silently drops the
    // environment's partition, which reads as "isolation randomly stopped
    // working" three screens later.
    if (candidate.is_valid() && !candidate.SchemeIs(content::kChromeUIScheme)) {
      start = candidate;
    } else {
      LOG(ERROR) << "ENVMGR: start_url rejected for " << spec.env_id << ": "
                 << spec.start_url;
    }
  }
  if (StartupCheckBlocked(spec.env_id)) {
    start = GURL(startup_gates_.at(spec.env_id).url);
  }
  content::NavigationController::LoadURLParams load_params{start};
  load_params.transition_type = ui::PAGE_TRANSITION_AUTO_TOPLEVEL;
  contents->GetController().LoadURLWithParams(load_params);
  return contents;
}

int EnvManager::StartupCheckPort() const {
  int port = 0;
  base::StringToInt(base::CommandLine::ForCurrentProcess()->GetSwitchValueASCII(
                       "lan-env-check-port"), &port);
  return port > 0 && port <= 65535 ? port : 0;
}

void EnvManager::EnsureStartupGate(const EnvProfile& spec) {
  if (!StartupCheckPort() || startup_gates_.contains(spec.env_id)) {
    return;
  }
  StartupGate gate;
  gate.key = base::UnguessableToken::Create().ToString();
  gate.url = base::StringPrintf("http://127.0.0.1:%d/?env=%s&key=%s",
      StartupCheckPort(), spec.env_id.c_str(), gate.key.c_str());
  gate.proxy = spec.proxy_config;
  gate.expected_ip = spec.expected_exit_ip;
  gate.destination = !spec.start_url.empty() ? spec.start_url :
      (!spec.homepage.empty() ? spec.homepage : kDefaultHomepage);
  startup_gates_.emplace(spec.env_id, std::move(gate));
}

bool EnvManager::StartupCheckBlocked(const std::string& env_id) const {
  if (!StartupCheckPort() || !FindEnv(env_id)) {
    return false;
  }
  auto gate = startup_gates_.find(env_id);
  return gate == startup_gates_.end() || !gate->second.unlocked;
}

// static
void EnvManager::AddStartupCheckThrottle(content::NavigationThrottleRegistry& registry) {
  if (!base::CommandLine::ForCurrentProcess()->HasSwitch("lan-env-check-port")) {
    return;
  }
  auto& handle = registry.GetNavigationHandle();
  if (handle.IsInPrimaryMainFrame() || handle.IsInPrerenderedMainFrame()) {
    registry.AddThrottle(std::make_unique<StartupCheckThrottle>(registry));
  }
}

bool EnvManager::BlockStartupNavigation(content::NavigationHandle* handle) {
  content::WebContents* contents = handle->GetWebContents();
  if (IsAssistantWebContents(contents) ||
      IsAssistantAuxiliaryWebContents(contents)) {
    return false;
  }
  std::string env_id = EnvIdOf(contents);
  if (env_id.empty() && contents->GetOpener()) {
    env_id = EnvIdOf(content::WebContents::FromRenderFrameHost(contents->GetOpener()));
  }
  if (env_id.empty()) {
    // Browser-created tabs can navigate before insertion into the tab strip,
    // before AdoptTabsIntoEnvironment has assigned their storage partition.
    // Gate that first request too; only the actual assistant host is exempt.
    env_id = active_env_id_;
  }
  if (!StartupCheckBlocked(env_id)) {
    return false;
  }
  auto gate = startup_gates_.find(env_id);
  if (gate != startup_gates_.end() &&
      handle->GetURL().GetWithoutRef() == GURL(gate->second.url)) {
    return false;
  }
  // Cancel before any request reaches the network, then restore the check page.
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(FROM_HERE,
      base::BindOnce(&EnvManager::ShowStartupCheck, weak_factory_.GetWeakPtr(), env_id));
  return true;
}

void EnvManager::ShowStartupCheck(const std::string& env_id) {
  const EnvProfile* spec = FindEnv(env_id);
  if (!spec || !StartupCheckBlocked(env_id)) {
    return;
  }
  EnsureStartupGate(*spec);
  content::WebContents* contents = FindTabForEnv(env_id);
  if (!contents) {
    std::string error;
    ActivateEnv(env_id, &error);
    return;
  }
  const GURL url(startup_gates_.at(env_id).url);
  if (contents->GetVisibleURL().GetWithoutRef() != url) {
    content::NavigationController::LoadURLParams params{url};
    contents->GetController().LoadURLWithParams(params);
  }
}

void EnvManager::CloseStartupEnvironment(const std::string& env_id) {
  BrowserWindowInterface* window = MainWindowIfReady();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip) {
    startup_gates_.erase(env_id);
    return;
  }
  base::AutoReset<bool> synchronizing(&group_sync_in_progress_, true);
  remember_tab_.erase(env_id);
  if (active_env_id_ == env_id) {
    active_env_id_.clear();
  }
  int count = 0;
  for (int i = 0; i < strip->count(); ++i) {
    if (EnvIdOf(strip->GetWebContentsAt(i)) == env_id) {
      ++count;
    }
  }
  if (count == strip->count()) {
    content::WebContents::CreateParams params(profile_);
    auto blank = content::WebContents::Create(params);
    blank->GetController().LoadURL(GURL(url::kAboutBlankURL), content::Referrer(),
                                  ui::PAGE_TRANSITION_AUTO_TOPLEVEL, std::string());
    strip->AddWebContents(std::move(blank), strip->count(),
                         ui::PAGE_TRANSITION_AUTO_TOPLEVEL, AddTabTypes::ADD_ACTIVE);
  }
  for (int i = strip->count() - 1; i >= 0; --i) {
    if (EnvIdOf(strip->GetWebContentsAt(i)) == env_id) {
      strip->CloseWebContentsAt(i, TabCloseTypes::CLOSE_NONE);
    }
  }
  startup_gates_.erase(env_id);
  if (strip->GetActiveWebContents()) {
    active_env_id_ = EnvIdOf(strip->GetActiveWebContents());
  }
  SaveToDisk();
  observers_.Notify(&Observer::OnEnvsChanged);
  observers_.Notify(&Observer::OnActiveEnvChanged, active_env_id_);
}

void EnvManager::HandleStartupCheck(const base::DictValue& command, std::string* result) {
  base::DictValue out;
  out.Set("ok", false);
  const std::string id = command.FindString("env_id") ? *command.FindString("env_id") : "";
  auto it = startup_gates_.find(id);
  EnvProfile* spec = FindMutableEnv(id);
  const std::string* key = command.FindString("key");
  if (!spec || it == startup_gates_.end() || !key || *key != it->second.key) {
    out.Set("error", "startup_check_expired");
    base::JSONWriter::Write(out, result);
    return;
  }
  StartupGate& gate = it->second;
  const std::string action = command.FindString("action") ? *command.FindString("action") : "";
  if (action == "inspect") {
    out.Set("ok", true);
    out.Set("env_id", id);
    out.Set("display_name", spec->display_name);
    out.Set("proxy", spec->proxy_config);
    out.Set("expected_exit_ip", spec->expected_exit_ip);
    out.Set("ip_mode", spec->ip_mode);
    out.Set("unlocked", gate.unlocked);
    out.Set("destination", gate.destination);
    out.Set("restart_required", gate.proxy != spec->proxy_config);
  } else if (action == "close") {
    out.Set("ok", true);
    base::SingleThreadTaskRunner::GetCurrentDefault()->PostDelayedTask(FROM_HERE,
        base::BindOnce(&EnvManager::CloseStartupEnvironment, weak_factory_.GetWeakPtr(), id),
        base::Milliseconds(150));
  } else if (action == "unlock") {
    const std::string* proxy = command.FindString("proxy");
    const std::string* expected = command.FindString("expected_exit_ip");
    const std::string decision = command.FindString("decision") ? *command.FindString("decision") : "";
    if (!proxy || !expected || *proxy != spec->proxy_config || *expected != spec->expected_exit_ip ||
        (decision != "pass" && decision != "continue_once" && decision != "dynamic" && decision != "baseline")) {
      out.Set("error", "startup_check_configuration_changed");
    } else {
      if (decision == "dynamic") {
        spec->ip_mode = "dynamic";
      } else if (decision == "baseline") {
        const std::string* ip = command.FindString("exit_ip");
        if (!ip || ip->empty()) {
          out.Set("error", "missing_exit_ip");
          base::JSONWriter::Write(out, result);
          return;
        }
        spec->expected_exit_ip = *ip;
      }
      gate.unlocked = true;
      SaveToDisk();
      observers_.Notify(&Observer::OnEnvsChanged);
      out.Set("ok", true);
      if (command.FindBool("navigate").value_or(false)) {
        const std::string destination = gate.destination;
        base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(FROM_HERE,
            base::BindOnce([](base::WeakPtr<EnvManager> manager, std::string env_id, std::string url) {
              if (manager) { std::string error; manager->NavigateEnv(env_id, url, &error); }
            }, weak_factory_.GetWeakPtr(), id, destination));
      }
    }
  } else {
    out.Set("error", "unknown_startup_check_action");
  }
  base::JSONWriter::Write(out, result);
}

bool EnvManager::NewTab(const std::string& env_id,
                        const std::string& url,
                        std::string* error) {
  const EnvProfile* existing = FindEnv(env_id);
  if (!existing) {
    *error = "unknown environment: " + env_id;
    return false;
  }
  if (StartupCheckBlocked(env_id) || (StartupCheckPort() && !startup_gates_.contains(env_id))) {
    ShowStartupCheck(env_id);
    *error = "environment_startup_check_required";
    return false;
  }
  // A copy: CreateTabForEnv does not touch `envs_`, and the caller's pointer must not be
  // assumed stable.
  EnvProfile spec = *existing;
  if (!url.empty()) {
    spec.start_url = url;
  } else {
    // New tab without an explicit target: the environment's home page (product default: Google).
    spec.start_url = existing->homepage.empty() ? kDefaultHomepage : existing->homepage;
  }
  std::unique_ptr<content::WebContents> contents = CreateTabForEnv(spec);
  if (!contents) {
    *error = "could not create a tab for " + env_id;
    return false;
  }

  // Every environment's tabs live in the one window: an inactive environment is a background
  // tab, which keeps loading and running.
  BrowserWindowInterface* window = EnsureMainWindow();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip) {
    *error = "no window for " + env_id;
    return false;
  }
  const bool is_active_env = (env_id == active_env_id_);
  content::WebContents* added = contents.get();
  strip->AddWebContents(std::move(contents), strip->count(),
                        ui::PAGE_TRANSITION_AUTO_TOPLEVEL,
                        is_active_env ? AddTabTypes::ADD_ACTIVE
                                      : AddTabTypes::ADD_NONE);
  if (is_active_env) {
    const int index = strip->GetIndexOfWebContents(added);
    if (index != TabStripModel::kNoTab) {
      strip->ActivateTabAt(index);
      base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvManager::FocusActivePage,
                                weak_factory_.GetWeakPtr()));
    }
  }
  LOG(ERROR) << "ENVMGR: newtab env=" << env_id << " active=" << is_active_env
             << " strip_now=" << strip->count();
  return true;
}

bool EnvManager::NavigateEnv(const std::string& env_id,
                             const std::string& url,
                             std::string* error) {
  GURL target(url);
  if (StartupCheckBlocked(env_id)) {
    ShowStartupCheck(env_id);
    *error = "environment_startup_check_required";
    return false;
  }
  if (!target.is_valid()) {
    *error = "invalid url: " + url;
    return false;
  }
  content::WebContents* contents = nullptr;
  BrowserWindowInterface* window = EnsureMainWindow();
  if (window) {
    contents = TabForEnv(env_id, window->GetTabStripModel());
  }
  if (!contents) {
    // No tab exists yet: remember the URL so the first tab opens it. This is the
    // case the CLI hits when it defines and activates an environment in one go.
    EnvProfile* spec = FindMutableEnv(env_id);
    if (!spec) {
      *error = "unknown environment: " + env_id;
      return false;
    }
    spec->start_url = url;
    SaveToDisk();
    return true;
  }
  content::NavigationController::LoadURLParams params{target};
  params.transition_type = ui::PAGE_TRANSITION_AUTO_TOPLEVEL;
  contents->GetController().LoadURLWithParams(params);
  LOG(ERROR) << "ENVMGR: navigate env=" << env_id << " url=" << url;
  return true;
}

bool EnvManager::ActivateEnv(const std::string& env_id, std::string* error) {
  if (!FindEnv(env_id)) {
    *error = "unknown environment: " + env_id;
    return false;
  }
  return ActivateEnvInWindow(env_id, error);
}

content::WebContents* EnvManager::TabForEnv(const std::string& env_id,
                                            TabStripModel* strip) const {
  if (!strip) {
    return nullptr;
  }
  for (int i = 0; i < strip->count(); ++i) {
    content::WebContents* contents = strip->GetWebContentsAt(i);
    if (contents && EnvIdOf(contents) == env_id) {
      return contents;
    }
  }
  return nullptr;
}


BrowserWindowInterface* EnvManager::MainWindowIfReady() const {
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile_);
  if (!collection) {
    return nullptr;
  }
  BrowserWindowInterface* found = nullptr;
  collection->ForEach(
      [&](BrowserWindowInterface* window) {
        TabStripModel* strip = window->GetTabStripModel();
        if (!strip) {
          return true;
        }
        for (int i = 0; i < strip->count(); ++i) {
          content::WebContents* contents = strip->GetWebContentsAt(i);
          if (contents && !EnvIdOf(contents).empty()) {
            found = window;
            return false;
          }
        }
        return true;
      },
      BrowserCollection::Order::kCreation);
  return found;
}

BrowserWindowInterface* EnvManager::EnsureMainWindow() {
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile_);
  if (!collection) {
    return nullptr;
  }
  // The main window is the one that already holds an environment tab; otherwise create it.
  BrowserWindowInterface* main_window = nullptr;
  BrowserWindowInterface* initial_window = nullptr;
  collection->ForEach(
      [&](BrowserWindowInterface* window) {
        TabStripModel* strip = window->GetTabStripModel();
        if (!strip) {
          return true;
        }
        // The product opens before the user creates any environments. Reuse
        // that window so first activation preserves its assistant and tabs.
        if (!initial_window && strip->count() > 0 && window->GetWindow() &&
            window->GetWindow()->IsVisible()) {
          initial_window = window;
        }
        for (int i = 0; i < strip->count(); ++i) {
          content::WebContents* contents = strip->GetWebContentsAt(i);
          if (contents && !EnvIdOf(contents).empty()) {
            main_window = window;
            return false;
          }
        }
        return true;
      },
      BrowserCollection::Order::kCreation);
  if (!main_window) {
    main_window = initial_window;
  }
  if (main_window) {
    // A window that was hidden in an earlier session is restored parked off screen; if it does not
    // overlap the work area, put it back where the canonical rect says.
    if (ui::BaseWindow* base = main_window->GetWindow()) {
      const gfx::Rect bounds = base->GetBounds();
      const gfx::Rect work =
          display::Screen::Get()->GetPrimaryDisplay().work_area();
      if (!bounds.Intersects(work) || bounds.width() < 400 ||
          bounds.height() < 300) {
        if (per_env_bounds_.IsEmpty()) {
          const int width = std::min(1400, work.width() - 40);
          const int height = std::min(900, work.height() - 40);
          per_env_bounds_ = gfx::Rect(
              work.x() + (work.width() - width) / 2,
              work.y() + (work.height() - height) / 2, width, height);
        }
        base->SetBounds(per_env_bounds_);
        LOG(ERROR) << "ENVMGR: recovered a parked window " << bounds.ToString()
                   << " -> " << per_env_bounds_.ToString();
      } else if (per_env_bounds_.IsEmpty()) {
        per_env_bounds_ = bounds;
      }
    }
    return main_window;
  }

  BrowserWindowInterface* window =
      CreateBrowserWindow(BrowserWindowCreateParams(profile_, true));
  if (!window || !window->GetTabStripModel()) {
    return nullptr;
  }
  if (per_env_bounds_.IsEmpty()) {
    display::Display display = display::Screen::Get()->GetPrimaryDisplay();
    const gfx::Rect work = display.work_area();
    const int width = std::min(1400, work.width() - 40);
    const int height = std::min(900, work.height() - 40);
    per_env_bounds_ = gfx::Rect(work.x() + (work.width() - width) / 2,
                                work.y() + (work.height() - height) / 2, width,
                                height);
  }
  if (ui::BaseWindow* base = window->GetWindow()) {
    base->SetBounds(per_env_bounds_);
  }
  LOG(ERROR) << "ENVMGR: created the main window";
  return window;
}

void EnvManager::HideTabStrip() {
  // The strip is not drawn at all: `BrowserView::ShouldDrawTabStrip()` returns false while this
  // manager hides it, which also reclaims the band's height. Nothing to do here.
  LOG(ERROR) << "ENVMGR: tab strip hidden=" << hide_tab_strip_;
}

void EnvManager::MergeIntoMainWindow(BrowserWindowInterface* main_window) {
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile_);
  if (!collection || !main_window || !main_window->GetTabStripModel()) {
    return;
  }
  TabStripModel* target = main_window->GetTabStripModel();

  std::vector<BrowserWindowInterface*> others;
  collection->ForEach(
      [&](BrowserWindowInterface* window) {
        if (window != main_window) {
          others.push_back(window);
        }
        return true;
      },
      BrowserCollection::Order::kCreation);

  for (BrowserWindowInterface* window : others) {
    TabStripModel* strip = window->GetTabStripModel();
    ui::BaseWindow* base = window->GetWindow();
    if (!strip || !base) {
      continue;
    }
    // Take the tabs over before the window closes; only idle tabs, so no navigation is disturbed.
    for (int i = strip->count() - 1; i >= 0; --i) {
      content::WebContents* contents = strip->GetWebContentsAt(i);
      if (!contents || EnvIdOf(contents).empty()) {
        continue;
      }
      if (contents->IsLoading()) {
        continue;
      }
      std::unique_ptr<content::WebContents> taken =
          strip->DetachWebContentsAtForInsertion(i);
      if (!taken) {
        continue;
      }
      target->AddWebContents(std::move(taken), target->count(),
                             ui::PAGE_TRANSITION_AUTO_TOPLEVEL, AddTabTypes::ADD_NONE);
      LOG(ERROR) << "ENVMGR: merged a tab into the main window";
    }
    // Tabs that belong to no environment cannot be merged (they have no proxy), so they are closed
    // rather than kept in a hidden window.
    for (int i = strip->count() - 1; i >= 0; --i) {
      content::WebContents* contents = strip->GetWebContentsAt(i);
      if (contents && EnvIdOf(contents).empty()) {
        strip->CloseWebContentsAt(i, TabCloseTypes::CLOSE_NONE);
      }
    }
    if (strip->count() == 0) {
      LOG(ERROR) << "ENVMGR: closing an emptied extra window";
      base->Close();
      continue;
    }
    // Anything left (tabs that were loading) stays in its window, which is then hidden rather than
    // closed so nothing is lost.
    if (base->IsVisible()) {
      base->Hide();
      LOG(ERROR) << "ENVMGR: hid an extra window instead of closing it";
    }
  }
}


std::vector<EnvManager::TabInfo> EnvManager::TabsForEnv(
    const std::string& env_id) const {
  std::vector<TabInfo> tabs;
  BrowserWindowInterface* window = MainWindowIfReady();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip) {
    return tabs;
  }
  const int active = strip->active_index();
  for (int i = 0; i < strip->count(); ++i) {
    content::WebContents* contents = strip->GetWebContentsAt(i);
    if (!contents || EnvIdOf(contents) != env_id) {
      continue;
    }
    TabInfo info;
    info.index = i;
    info.title = contents->GetTitle();
    if (info.title.empty()) {
      info.title = base::UTF8ToUTF16(contents->GetVisibleURL().spec());
    }
    info.active = (i == active);
    tabs.push_back(std::move(info));
  }
  return tabs;
}

void EnvManager::FocusActivePage() {
  BrowserWindowInterface* window = MainWindowIfReady();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  content::WebContents* active = strip ? strip->GetActiveWebContents() : nullptr;
  if (!active) {
    return;
  }
  // A Tab view holding views focus paints its keyboard focus ring on the active tab; official Chrome
  // only shows that ring while the user navigates by keyboard. Drop views focus, then focus the page -
  // the state a real click ends in. Runs as a posted task because ActivateTabAt itself focuses the tab.
  if (ui::BaseWindow* base = window->GetWindow()) {
    if (views::Widget* widget =
            views::Widget::GetWidgetForNativeWindow(base->GetNativeWindow())) {
      if (views::FocusManager* focus_manager = widget->GetFocusManager()) {
        focus_manager->ClearFocus();
      }
    }
  }
  active->Focus();
  LOG(ERROR) << "ENVMGR: focus handed to the page after activation";
}

bool EnvManager::ActivateTabInEnv(const std::string& env_id, int index) {
  BrowserWindowInterface* window = EnsureMainWindow();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip || index < 0 || index >= strip->count()) {
    return false;
  }
  content::WebContents* contents = strip->GetWebContentsAt(index);
  if (!contents || EnvIdOf(contents) != env_id) {
    return false;
  }
  strip->ActivateTabAt(index);
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvManager::FocusActivePage,
                                weak_factory_.GetWeakPtr()));
  remember_tab_[env_id] = strip->GetActiveWebContents();
  SaveToDisk();
  observers_.Notify(&Observer::OnTabsChanged);
  return true;
}

bool EnvManager::CloseTabInEnv(const std::string& env_id, int index) {
  BrowserWindowInterface* window = EnsureMainWindow();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip || index < 0 || index >= strip->count()) {
    return false;
  }
  content::WebContents* contents = strip->GetWebContentsAt(index);
  if (!contents || EnvIdOf(contents) != env_id) {
    return false;
  }
  strip->CloseWebContentsAt(index, TabCloseTypes::CLOSE_NONE);
  observers_.Notify(&Observer::OnTabsChanged);
  return true;
}


std::optional<tab_groups::TabGroupId> EnvManager::EnsureEnvGroup(
    const std::string& env_id,
    TabStripModel* strip) {
  if (!strip || !strip->SupportsTabGroups() ||
      base::CommandLine::ForCurrentProcess()->HasSwitch("lan-no-env-groups")) {
    return std::nullopt;
  }
  // Group operations reorder indices. Retain WebContents identities across them.
  std::vector<content::WebContents*> members;
  std::optional<tab_groups::TabGroupId> group;
  for (int i = 0; i < strip->count(); ++i) {
    content::WebContents* tab = strip->GetWebContentsAt(i);
    if (EnvIdOf(tab) == env_id) {
      members.push_back(tab);
      if (!group.has_value()) {
        group = strip->GetTabGroupForTab(i);
      }
    }
  }
  if (members.empty()) {
    env_groups_.erase(env_id);
    return std::nullopt;
  }
  if (!group.has_value()) {
    group = tab_groups::TabGroupId::GenerateNew();
    // Register before notifying Views: header width must be neutral on creation.
    RegisterEnvGroup(*group);
    strip->AddToGroupForRestore(
        {strip->GetIndexOfWebContents(members.front())}, *group);
  }
  RegisterEnvGroup(*group);
  env_groups_.insert_or_assign(env_id, *group);
  std::vector<int> indices;
  for (content::WebContents* tab : members) {
    const int index = strip->GetIndexOfWebContents(tab);
    if (index != TabStripModel::kNoTab && strip->GetTabGroupForTab(index) != group) {
      indices.push_back(index);
    }
  }
  std::ranges::sort(indices);
  if (!indices.empty()) {
    strip->AddToExistingGroup(indices, *group);
  }
  return group;
}

void EnvManager::SetGroupsCollapsedFor(const std::string& keep_env_id,
                                      TabStripModel* strip) {
  if (!strip || !strip->SupportsTabGroups()) {
    return;
  }
  for (const auto& [env_id, group] : env_groups_) {
    if (!strip->group_model()->ContainsTabGroup(group)) {
      continue;
    }
    const bool collapse = env_id != keep_env_id;
    if (strip->IsGroupCollapsed(group) == collapse) {
      continue;
    }
    strip->ChangeTabGroupVisuals(
        group, tab_groups::TabGroupVisualData(base::UTF8ToUTF16(env_id),
                                           tab_groups::TabGroupColorId::kGrey,
                                           collapse));
  }
}

void EnvManager::ScheduleGroupSync() {
  if (group_sync_in_progress_ || group_sync_pending_) {
    return;
  }
  group_sync_pending_ = true;
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvManager::SynchronizeEnvGroups,
                                weak_factory_.GetWeakPtr()));
}

void EnvManager::SynchronizeEnvGroups() {
  group_sync_pending_ = false;
  if (group_sync_in_progress_) {
    return;
  }
  BrowserWindowInterface* window = MainWindowIfReady();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip || strip->empty()) {
    return;
  }
  base::AutoReset<bool> synchronizing(&group_sync_in_progress_, true);
  for (auto it = startup_gates_.begin(); it != startup_gates_.end();) {
    if (!FindTabForEnv(it->first)) {
      it = startup_gates_.erase(it);
    } else {
      ++it;
    }
  }
  // Native close/keyboard selection may select a different environment. Keep the
  // sidebar and group visibility consistent with the page actually on screen.
  content::WebContents* selected = strip->GetActiveWebContents();
  const std::string selected_env = EnvIdOf(selected);
  const bool changed = !selected_env.empty() && selected_env != active_env_id_;
  if (!selected_env.empty()) {
    active_env_id_ = selected_env;
    remember_tab_[selected_env] = selected;
  }
  for (const EnvProfile& spec : envs_) {
    EnsureEnvGroup(spec.env_id, strip);
  }
  SetGroupsCollapsedFor(active_env_id_, strip);
  if (changed) {
    SaveToDisk();
    observers_.Notify(&Observer::OnActiveEnvChanged, active_env_id_);
  }
}

bool EnvManager::PrepareStartupWindow(BrowserWindowInterface* window) {
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip || strip->count() || envs_.empty() ||
      window->GetProfile() != profile_) {
    return false;
  }
  const EnvProfile* target = FindEnv(active_env_id_);
  if (!target) {
    target = &envs_.front();
  }
  EnvProfile spec = *target;
  if (spec.start_url.empty()) {
    spec.start_url = spec.homepage.empty() ? kDefaultHomepage : spec.homepage;
  }
  auto contents = CreateTabForEnv(spec);
  if (!contents) {
    return false;
  }
  active_env_id_ = spec.env_id;
  base::AutoReset<bool> synchronizing(&group_sync_in_progress_, true);
  AttachToStrip(strip);
  strip->AddWebContents(std::move(contents), 0,
                       ui::PAGE_TRANSITION_AUTO_TOPLEVEL, AddTabTypes::ADD_ACTIVE);
  EnsureEnvGroup(spec.env_id, strip);
  if (EnvProfile* active = FindMutableEnv(spec.env_id)) {
    active->used = true;
  }
  SaveToDisk();
  // Build the product sidebar before the first Show(), so there is no visible
  // bootstrap window followed by an environment window.
  ShowAssistantLayer(window);
  observers_.Notify(&Observer::OnEnvsChanged);
  observers_.Notify(&Observer::OnActiveEnvChanged, active_env_id_);
  LOG(ERROR) << "ENVMGR: product startup prepared before first show env="
             << active_env_id_ << " tabs=" << strip->count();
  return true;
}

bool EnvManager::ActivateEnvInWindow(const std::string& env_id,
                                      std::string* error) {
  const EnvProfile* target = FindEnv(env_id);
  if (!target) {
    *error = "unknown environment: " + env_id;
    return false;
  }
  BrowserWindowInterface* window = EnsureMainWindow();
  TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
  if (!strip) {
    *error = "no main window";
    return false;
  }
  base::AutoReset<bool> synchronizing(&group_sync_in_progress_, true);
  MergeIntoMainWindow(window);
  const std::string previous = active_env_id_;
  content::WebContents* selected = strip->GetActiveWebContents();
  if (selected && !EnvIdOf(selected).empty()) {
    remember_tab_[EnvIdOf(selected)] = selected;
  }
  content::WebContents* wanted = nullptr;
  auto remembered = remember_tab_.find(env_id);
  if (remembered != remember_tab_.end() &&
      strip->GetIndexOfWebContents(remembered->second) != TabStripModel::kNoTab &&
      EnvIdOf(remembered->second) == env_id) {
    wanted = remembered->second;
  }
  if (!wanted) {
    wanted = TabForEnv(env_id, strip);
  }
  active_env_id_ = env_id;
  if (!wanted) {
    auto fresh = CreateTabForEnv(*target);
    if (!fresh) {
      *error = "could not create a tab for " + env_id;
      active_env_id_ = previous;
      return false;
    }
    wanted = fresh.get();
    strip->AddWebContents(std::move(fresh), strip->count(),
                         ui::PAGE_TRANSITION_AUTO_TOPLEVEL, AddTabTypes::ADD_NONE);
  }
  for (const EnvProfile& spec : envs_) {
    EnsureEnvGroup(spec.env_id, strip);
  }
  const auto group = strip->GetTabGroupForTab(strip->GetIndexOfWebContents(wanted));
  if (group && strip->IsGroupCollapsed(*group)) {
    strip->ChangeTabGroupVisuals(
        *group, tab_groups::TabGroupVisualData(base::UTF8ToUTF16(env_id),
                                            tab_groups::TabGroupColorId::kGrey,
                                            false));
  }
  const int index = strip->GetIndexOfWebContents(wanted);
  strip->ActivateTabAt(index);
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvManager::FocusActivePage,
                                weak_factory_.GetWeakPtr()));
  SetGroupsCollapsedFor(env_id, strip);
  if (ui::BaseWindow* base = window->GetWindow()) {
    if (!base->IsVisible()) {
      base->Show();
    }
    base->Activate();
  }
  if (panel_request_handler_) {
    panel_request_handler_.Run();
  }
  if (EnvProfile* mutable_target = FindMutableEnv(env_id)) {
    mutable_target->used = true;
  }
  SaveToDisk();
  observers_.Notify(&Observer::OnEnvsChanged);
  observers_.Notify(&Observer::OnActiveEnvChanged, env_id);
  return true;
}

void EnvManager::AttachToStrip(TabStripModel* strip) {
  if (!strip || observed_strip_ == strip) {
    return;
  }
  // Browser::OnTabInsertedAt also runs for the assistant's authorization
  // popup. Do not replace the main window's observation with a short-lived
  // login strip (it is destroyed on cancel and would leave a dangling pointer).
  for (int i = 0; i < strip->count(); ++i) {
    if (IsAssistantAuxiliaryWebContents(strip->GetWebContentsAt(i))) {
      return;
    }
  }
  if (observed_strip_) {
    observed_strip_->RemoveObserver(this);
  }
  observed_strip_ = strip;
  observed_strip_->AddObserver(this);
  LOG(ERROR) << "ENVMGR: observing the tab strip for new tabs";
}

void EnvManager::OnTabStripModelChanged(
    TabStripModel* tab_strip_model,
    const TabStripModelChange& change,
    const TabStripSelectionChange& selection) {
  ScheduleGroupSync();
  // The tab strip is hidden, so the panel's tab bar is the only place the user can see tabs.
  if (change.type() != TabStripModelChange::kSelectionOnly) {
    observers_.Notify(&Observer::OnTabsChanged);
  }
  if (change.type() != TabStripModelChange::kInserted ||
      active_env_id_.empty()) {
    return;
  }
  for (const TabStripModelChange::ContentsWithIndex& inserted :
       change.GetInsert()->contents) {
    content::WebContents* contents = inserted.contents;
    if (!contents || !EnvIdOf(contents).empty() ||
        IsAssistantAuxiliaryWebContents(contents)) {
      continue;
    }
    // chrome:// pages stay where they are: WebUI needs the default partition and
    // has no environment identity to preserve.
    const GURL& url = contents->GetLastCommittedURL();
    if (url.SchemeIs(content::kChromeUIScheme)) {
      continue;
    }
    pending_adopt_env_[contents] = active_env_id_;
    if (tab_strip_model->active_index() == inserted.index) {
      pending_adopt_active_.insert(contents);
    }
  }
  if (pending_adopt_env_.empty()) {
    return;
  }
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvManager::AdoptTabs,
                                weak_factory_.GetWeakPtr()));
}

void EnvManager::AdoptTabs() {
  std::map<content::WebContents*, std::string> pending;
  pending.swap(pending_adopt_env_);
  std::set<content::WebContents*> was_active;
  was_active.swap(pending_adopt_active_);
  TabStripModel* strip = observed_strip_;
  if (!strip) {
    return;
  }
  for (const auto& [tab, env_at_insert] : pending) {
    content::WebContents* contents = tab;
    const std::string env_id = env_at_insert;
    const EnvProfile* spec = env_id.empty() ? nullptr : FindEnv(env_id);
    if (!spec) {
      continue;
    }
    const bool keep_selected = was_active.count(contents) > 0;
    int index = strip->GetIndexOfWebContents(contents);
    if (index == TabStripModel::kNoTab) {
      continue;  // closed in the meantime
    }
    if (StartupCheckBlocked(env_id) && strip->count() > 1) {
      strip->CloseWebContentsAt(index, TabCloseTypes::CLOSE_NONE);
      ShowStartupCheck(env_id);
      continue;
    }
    if (!EnvIdOf(contents).empty()) {
      continue;  // already belongs to an environment
    }
    GURL url = contents->GetLastCommittedURL();
    if (url.is_empty()) {
      // A tab that is still loading has no committed URL yet; without this the
      // adopted tab opened about:blank and lost the page the user asked for
      // (measured: "plain tab -> replacement at about:blank").
      url = contents->GetVisibleURL();
    }
    if (url.is_empty() || url.SchemeIs(content::kChromeUIScheme)) {
      // The strip's + opens chrome://newtab; the replacement must land on the home page.
      const std::string& home =
          !spec->homepage.empty() ? spec->homepage : kDefaultHomepage;
      url = GURL(home);
      LOG(ERROR) << "ENVMGR: new tab -> home page " << url.spec();
    }
    EnvProfile tab_spec = *spec;
    tab_spec.start_url = url.spec();
    LOG(ERROR) << "ENVMGR: adopting a default-partition tab into " << env_id
               << " url=" << url.spec().substr(0, 80);
    // Skip the only tab: replacing it would momentarily empty the strip, which
    // schedules the window for deletion. Measured with the startup tab: detach
    // first emptied the strip and the browser died in
    // `Browser::OnTabInsertedAt` with "Check failed: !IsDeleteScheduled()".
    if (strip->count() <= 1) {
      LOG(ERROR) << "ENVMGR: not adopting the only tab (" << env_id
                 << "); activation owns that case";
      continue;
    }
    std::unique_ptr<content::WebContents> replacement =
        CreateTabForEnv(tab_spec);
    if (!replacement) {
      LOG(ERROR) << "ENVMGR: adoption stopped, CreateTabForEnv returned null";
      continue;
    }
    LOG(ERROR) << "ENVMGR: adoption built a replacement env="
               << EnvIdOf(replacement.get()) << " strip=" << strip->count()
               << " index=" << index;
    // Insert before detaching: the strip must never be empty. The replacement lands
    // after the original and takes its index when the original is detached.
    // The replacement must be the selected tab when the original was: a new tab that does not
    // become active is what the operator reported as "clicking + opens a tab somewhere else".
    strip->AddWebContents(std::move(replacement), index + 1,
                          ui::PAGE_TRANSITION_AUTO_TOPLEVEL,
                          keep_selected ? AddTabTypes::ADD_ACTIVE
                                        : AddTabTypes::ADD_NONE);
    std::unique_ptr<content::WebContents> original =
        strip->DetachWebContentsAtForInsertion(index);
    LOG(ERROR) << "ENVMGR: adoption replaced the tab, strip now " << strip->count();
    // `original` is destroyed here -- the leak is closed, the environment tab in
    // its place carries the proxy.
  }
}

void EnvManager::SetPanelTab(std::string tab) {
  if (panel_tab_ == tab) {
    return;
  }
  panel_tab_ = std::move(tab);
  SaveToDisk();
}

std::string EnvManager::LocaleForPartition(
    const content::StoragePartitionConfig& partition_config) const {
  const std::string& name = partition_config.partition_name();
  if (!base::StartsWith(name, kPartitionNamePrefix)) {
    return std::string();
  }
  const EnvProfile* found = FindEnv(name.substr(kPartitionNamePrefix.size()));
  if (!found) {
    return std::string();
  }
  if (!found->accept_languages.empty()) {
    const size_t comma = found->accept_languages.find(',');
    return std::string(base::TrimWhitespaceASCII(
        found->accept_languages.substr(0, comma), base::TRIM_ALL));
  }
  return found->locale.empty() ? "en-US" : found->locale;
}

std::string EnvManager::AcceptLanguagesForPartition(
    const content::StoragePartitionConfig& partition_config) const {
  const std::string& name = partition_config.partition_name();
  if (!base::StartsWith(name, kPartitionNamePrefix)) {
    return std::string();
  }
  const EnvProfile* found = FindEnv(name.substr(kPartitionNamePrefix.size()));
  if (!found) {
    return std::string();
  }
  if (!found->accept_languages.empty()) {
    return found->accept_languages;
  }
  return found->locale.empty() ? "en-US,en" : found->locale;
}

std::string EnvManager::FingerprintLanguagesForPartition(
    const content::StoragePartitionConfig& partition_config) const {
  return AcceptLanguagesForPartition(partition_config);
}

std::string EnvManager::ProxyRulesForPartition(
    const content::StoragePartitionConfig& partition_config) const {
  // Uses the partition name, not the on-disk path: Chromium hashes the name into
  // a hex directory, so the path carries no recoverable identity.
  const std::string& name = partition_config.partition_name();
  if (!base::StartsWith(name, kPartitionNamePrefix)) {
    return std::string();
  }
  const EnvProfile* found = FindEnv(name.substr(kPartitionNamePrefix.size()));
  return found ? found->proxy_config : std::string();
}

std::string EnvManager::SeedForPartition(
    const content::StoragePartitionConfig& partition_config) const {
  if (partition_config.is_default()) {
    return std::string();
  }
  const std::string& name = partition_config.partition_name();
  if (!base::StartsWith(name, kPartitionNamePrefix)) {
    return std::string();
  }
  const EnvProfile* found = FindEnv(name.substr(kPartitionNamePrefix.size()));
  return found ? found->ResolvedSeed() : std::string();
}

base::ListValue EnvManager::LiveTabsJson(const std::string& env_id) const {
  // The DevTools target id is the only exact handle automation can use to address a
  // tab: url/title can repeat and visibility cannot distinguish two windows.
  base::ListValue tabs;
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile_);
  if (!collection) {
    return tabs;
  }
  // GetBrowsers() is protected, so walk through the public ForEach() helper.
  collection->ForEach(
      [&](BrowserWindowInterface* window) {
        TabStripModel* strip = window->GetTabStripModel();
        if (!strip) {
          return true;
        }
        for (int i = 0; i < strip->count(); ++i) {
          content::WebContents* contents = strip->GetWebContentsAt(i);
          if (!contents || EnvIdOf(contents) != env_id) {
            continue;
          }
          base::DictValue tab;
          tab.Set("url", contents->GetVisibleURL().spec());
          tab.Set("title", base::UTF16ToUTF8(contents->GetTitle()));
          tab.Set("target_id",
                  content::DevToolsAgentHost::GetOrCreateFor(contents)->GetId());
          // The partition is the tab's identity: reporting it lets verification assert on data instead of guessing
          // which DevTools target is "the one the product adopted".
          if (content::SiteInstance* site_instance = contents->GetSiteInstance()) {
            const content::StoragePartitionConfig& partition =
                site_instance->GetSecurityPrincipal().GetStoragePartitionConfig();
            tab.Set("partition", partition.partition_name());
            tab.Set("partition_default", partition.is_default());
          }
          tabs.Append(std::move(tab));
        }
        return true;
      },
      BrowserCollection::Order::kCreation);
  return tabs;
}

std::string EnvManager::DescribeStateJson() const {
  base::ListValue list;
  for (const EnvProfile& e : envs_) {
    base::DictValue d;
    d.Set("env_id", e.env_id);
    d.Set("display_name", e.display_name.empty() ? e.env_id : e.display_name);
    d.Set("group", e.group);
    d.Set("tags", e.tags);
    d.Set("notes", e.notes);
    d.Set("partition", e.PartitionName());
    d.Set("proxy", e.proxy_config);
    d.Set("expected_exit_ip", e.expected_exit_ip);
    d.Set("ip_mode", e.ip_mode);
    d.Set("start_url", e.start_url);
    d.Set("homepage", e.homepage);
    d.Set("fingerprint_seed", e.fingerprint_seed);
    d.Set("locale", e.locale);
    d.Set("accept_languages", e.accept_languages);
    d.Set("active", e.env_id == active_env_id_);
    d.Set("used", e.used);
    // No parked tabs any more: every environment's tabs live in its own window.
    d.Set("detached_tabs", 0);
    d.Set("tabs", LiveTabsJson(e.env_id));
    list.Append(std::move(d));
  }
  base::DictValue root;
  root.Set("active", active_env_id_);
  root.Set("panel_tab", panel_tab_);
  root.Set("window_per_env", window_per_env_);
  root.Set("envs", std::move(list));
  std::string json;
  base::JSONWriter::Write(root, &json);
  return json;
}

std::string EnvManager::DescribeRuntimeJson() const {
  base::ListValue running;
  std::set<std::string> seen;
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile_);
  if (collection) {
    collection->ForEach(
        [&](BrowserWindowInterface* window) {
          TabStripModel* strip = window->GetTabStripModel();
          if (!strip) {
            return true;
          }
          for (int i = 0; i < strip->count(); ++i) {
            const std::string env_id = EnvIdOf(strip->GetWebContentsAt(i));
            if (!env_id.empty() && seen.insert(env_id).second) {
              running.Append(env_id);
            }
          }
          return true;
        },
        BrowserCollection::Order::kCreation);
  }
  base::DictValue root;
  root.Set("ok", true);
  root.Set("active", active_env_id_);
  root.Set("running_envs", std::move(running));
  std::string json;
  base::JSONWriter::Write(root, &json);
  return json;
}

// ---------------------------------------------------------------------------
// Persistence and the command channel
// ---------------------------------------------------------------------------

void EnvManager::LoadFromDisk() {
  const base::FilePath path = profile_->GetPath().AppendASCII(kStateFileName);
  std::string raw;
  if (!base::ReadFileToString(path, &raw) || raw.empty()) {
    return;
  }
  std::optional<base::Value> parsed = base::JSONReader::Read(raw, base::JSON_PARSE_RFC);
  if (!parsed || !parsed->is_dict()) {
    LOG(ERROR) << "ENVMGR: " << kStateFileName << " is not a JSON object";
    return;
  }
  const base::DictValue& dict = parsed->GetDict();
  if (const std::string* active = dict.FindString("active")) {
    active_env_id_ = *active;
  }
  if (const std::string* tab = dict.FindString("panel_tab")) {
    panel_tab_ = *tab;
  }
  if (std::optional<bool> per_env = dict.FindBool("window_per_env")) {
    window_per_env_ = *per_env;
  }
  const base::ListValue* list = dict.FindList("envs");
  if (!list) {
    return;
  }
  for (const base::Value& item : *list) {
    if (!item.is_dict()) {
      continue;
    }
    const base::DictValue& d = item.GetDict();
    EnvProfile e;
    if (const std::string* v = d.FindString("env_id")) {
      e.env_id = *v;
    }
    if (e.env_id.empty()) {
      continue;
    }
    if (const std::string* v = d.FindString("display_name")) {
      e.display_name = *v;
    }
    if (const std::string* v = d.FindString("group")) {
      e.group = *v;
    }
    if (const std::string* v = d.FindString("tags")) {
      e.tags = *v;
    }
    if (const std::string* v = d.FindString("notes")) {
      e.notes = *v;
    }
    if (const std::string* v = d.FindString("proxy")) {
      e.proxy_config = *v;
    }
    if (const std::string* v = d.FindString("start_url")) {
      e.start_url = *v;
    }
    if (const std::string* v = d.FindString("homepage")) {
      e.homepage = *v;
    }
    if (const std::string* v = d.FindString("fingerprint_seed")) {
      e.fingerprint_seed = *v;
    }
    if (const std::string* v = d.FindString("expected_exit_ip")) {
      e.expected_exit_ip = *v;
    }
    if (const std::string* v = d.FindString("ip_mode")) {
      e.ip_mode = *v == "dynamic" ? "dynamic" : "static";
    }

    if (const std::string* v = d.FindString("locale")) {
      e.locale = *v;
    }
    if (const std::string* v = d.FindString("accept_languages")) {
      e.accept_languages = *v;
    }
    if (std::optional<bool> v = d.FindBool("used")) {
      e.used = *v;
    }
    envs_.push_back(std::move(e));
  }
}

void EnvManager::SaveToDisk() const {
  base::ListValue list;
  for (const EnvProfile& e : envs_) {
    base::DictValue d;
    d.Set("env_id", e.env_id);
    d.Set("display_name", e.display_name);
    d.Set("group", e.group);
    d.Set("tags", e.tags);
    d.Set("notes", e.notes);
    d.Set("proxy", e.proxy_config);
    d.Set("start_url", e.start_url);
    d.Set("homepage", e.homepage);
    d.Set("fingerprint_seed", e.fingerprint_seed);
    d.Set("expected_exit_ip", e.expected_exit_ip);
    d.Set("ip_mode", e.ip_mode);
    d.Set("locale", e.locale);
    d.Set("accept_languages", e.accept_languages);
    d.Set("used", e.used);
    list.Append(std::move(d));
  }
  base::DictValue root;
  root.Set("active", active_env_id_);
  root.Set("panel_tab", panel_tab_);
  root.Set("envs", std::move(list));
  std::string json;
  base::JSONWriter::Write(root, &json);
  const base::FilePath path = profile_->GetPath().AppendASCII(kStateFileName);
  if (!base::WriteFile(path, json)) {
    LOG(ERROR) << "ENVMGR: could not write " << kStateFileName;
  }
}

void EnvManager::PollCommandFile() {
  const base::FilePath dir = profile_->GetPath();
  const base::FilePath cmd = dir.AppendASCII(kCommandFileName);
  if (!base::PathExists(cmd)) {
    return;
  }
  std::string raw;
  if (!base::ReadFileToString(cmd, &raw)) {
    return;
  }
  // Claim the command before running it so a failure cannot loop forever.
  if (!base::DeleteFile(cmd)) {
    LOG(ERROR) << "ENVMGR: could not delete command file";
    return;
  }
  std::string result;
  HandleCommand(raw, &result);

  // Tag the reply with its request id. Several producers share this channel (the bridge polls `list` about once a
  // second) and without the tag a producer can read another one's reply - measured as `overlay query` returning an
  // environment list. Ids are hex, so no escaping is needed.
  std::optional<base::Value> request = base::JSONReader::Read(raw, base::JSON_PARSE_RFC);
  if (request && request->is_dict()) {
    if (const std::string* id = request->GetDict().FindString("request_id");
        id && !id->empty() && result.size() > 1 && result[0] == '{') {
      std::string prefix = R"("request_id":")";
      result.insert(1, prefix + *id + R"(",)");
    }
  }

  const base::FilePath out = dir.AppendASCII(kResultFileName);
  base::WriteFile(out, result);
  LOG(ERROR) << "ENVMGR: command handled, result=" << result.substr(0, 120);
}

content::WebContents* EnvManager::FindTabForEnv(const std::string& env_id) const {
  ProfileBrowserCollection* collection =
      ProfileBrowserCollection::GetForProfile(profile_);
  if (!collection) {
    return nullptr;
  }
  content::WebContents* found = nullptr;
  collection->ForEach([&](BrowserWindowInterface* window) {
    TabStripModel* strip = window->GetTabStripModel();
    if (!strip) {
      return true;
    }
    for (int i = 0; i < strip->count(); ++i) {
      content::WebContents* contents = strip->GetWebContentsAt(i);
      if (contents && EnvIdOf(contents) == env_id) {
        found = contents;
        return false;
      }
    }
    return true;
  });
  return found;
}



std::u16string EnvManager::DisplayNameForTab(content::WebContents* contents) const {
  const std::string env_id = EnvIdOf(contents);
  if (env_id.empty()) {
    return std::u16string();
  }
  const EnvProfile* spec = FindEnv(env_id);
  return base::UTF8ToUTF16(spec && !spec->display_name.empty() ? spec->display_name
                                                              : env_id);
}


void EnvManager::MaybeStartAutomationServer() {
  const base::CommandLine* command_line = base::CommandLine::ForCurrentProcess();
  const std::string port_value =
      command_line->GetSwitchValueASCII("lan-automation-port");
  const std::string token =
      command_line->GetSwitchValueASCII("lan-automation-token");
  if (port_value.empty() || token.empty()) {
    return;
  }
  int port = 0;
  if (!base::StringToInt(port_value, &port) || port <= 0) {
    LOG(ERROR) << "ENVAUTO: bad --lan-automation-port=" << port_value;
    return;
  }
  auto* impl = new AutomationTaskImpl(
      automation_task_.BindNewPipeAndPassReceiver(),
      base::BindRepeating(
          [](EnvManager* manager, const std::string& env_id)
              -> content::RenderFrameHost* {
            content::WebContents* contents = manager->FindTabForEnv(env_id);
            return contents ? contents->GetPrimaryMainFrame() : nullptr;
          },
          base::Unretained(this)));
  automation_task_impl_.reset(impl);
  automation_server_ = std::make_unique<AutomationServer>(
      profile_, std::move(automation_task_));
  automation_server_->Start(port, token);
}

void EnvManager::HandleCommand(const std::string& json, std::string* result) {
  std::optional<base::Value> parsed = base::JSONReader::Read(json, base::JSON_PARSE_RFC);
  if (!parsed || !parsed->is_dict()) {
    *result = "{\"ok\":false,\"error\":\"command is not a JSON object\"}";
    return;
  }
  const base::DictValue& d = parsed->GetDict();
  const std::string* command = d.FindString("command");
  if (!command) {
    *result = "{\"ok\":false,\"error\":\"missing command\"}";
    return;
  }

  if (*command == "startup-check") {
    HandleStartupCheck(d, result);
    return;
  }

  if (*command == "task") {
    // One IPC for the whole op list: the measured cost of a browser-side round trip is
    // ~2.8 ms, so batching is where the speed comes from (design doc §30).
    const std::string* env_id = d.FindString("env_id");
    const base::ListValue* ops = d.FindList("ops");
    content::WebContents* contents = env_id ? FindTabForEnv(*env_id) : nullptr;
    content::RenderFrameHost* frame =
        contents ? contents->GetPrimaryMainFrame() : nullptr;
    if (!frame || !ops) {
      *result = "{\"ok\":false,\"error\":\"need env_id with a live tab and ops\"}";
      return;
    }
    const size_t op_count = ops->size();
    std::string payload;
    base::JSONWriter::Write(*ops, &payload);
    const std::u16string script = base::UTF8ToUTF16(
        "(() => { const ops = " + payload +
        "; const out = []; for (const op of ops) { try {"
        " if (op.click !== undefined) { const el = document.querySelector(op.click);"
        "   if (el) { el.click(); out.push({click: true}); } else out.push({click: false}); }"
        " else if (op.read !== undefined) { const el = document.querySelector(op.read);"
        "   out.push({read: el ? (el.innerText || el.value || '').slice(0, op.limit || 200)"
        "                      : null}); }"
        " else if (op.type !== undefined) { const el = document.querySelector(op.type.selector);"
        "   if (el) { el.focus(); el.value = op.type.text;"
        "     el.dispatchEvent(new Event('input', {bubbles: true})); out.push({type: true}); }"
        "   else out.push({type: false}); }"
        " else if (op.observe !== undefined) {"
        "   const nodes = Array.from(document.querySelectorAll('a[href],button,input,select'))"
        "     .slice(0, op.limit || 20);"
        "   out.push({observe: nodes.map((el) => ({ text:"
        "     (el.innerText || el.value || '').trim().slice(0, 30) }))}); }"
        " else { out.push({error: 'unknown op'}); }"
        " } catch (e) { out.push({error: String(e).slice(0, 120)}); } }"
        " return JSON.stringify({url: location.href, results: out}); })()");

    const base::FilePath out_path =
        profile_->GetPath().AppendASCII("env-task-result.json");
    const base::TimeTicks started = base::TimeTicks::Now();
    frame->ExecuteJavaScriptInIsolatedWorld(
        script,
        base::BindOnce(
            [](base::TimeTicks began, base::FilePath path, size_t count,
               base::Value value) {
              const double elapsed = (base::TimeTicks::Now() - began).InMillisecondsF();
              base::DictValue payload_out;
              payload_out.Set("ok", true);
              payload_out.Set("ops", static_cast<int>(count));
              payload_out.Set("ms", elapsed);
              payload_out.Set("ms_per_op", count ? elapsed / count : 0.0);
              if (value.is_string()) {
                std::optional<base::Value> parsed_inner =
                    base::JSONReader::Read(value.GetString(), base::JSON_PARSE_RFC);
                if (parsed_inner) {
                  payload_out.Set("page", std::move(*parsed_inner));
                }
              }
              std::string json;
              base::JSONWriter::Write(payload_out, &json);
              base::WriteFile(path, json);
              LOG(ERROR) << "ENVTASK: ops=" << count << " total=" << elapsed
                         << "ms per_op=" << (count ? elapsed / count : 0.0) << "ms";
            },
            started, out_path, op_count),
        /*world_id=*/1);

    base::DictValue out;
    out.Set("ok", true);
    out.Set("async", true);
    out.Set("ops", static_cast<int>(op_count));
    base::JSONWriter::Write(out, result);
    return;
  }

  if (*command == "bench") {
    // P2b probe: how fast is an in-browser batch compared with the CDP path? The N
    // evaluations are chained through their callbacks -- a nested run loop on the UI thread
    // never sees the reply (measured: 60 s timeout).
    const std::string* env_id = d.FindString("env_id");
    const int iterations = d.FindInt("iterations").value_or(20);
    content::WebContents* contents = env_id ? FindTabForEnv(*env_id) : nullptr;
    content::RenderFrameHost* frame =
        contents ? contents->GetPrimaryMainFrame() : nullptr;
    if (!frame) {
      *result = "{\"ok\":false,\"error\":\"no tab for that environment\"}";
      return;
    }
    auto state = std::make_shared<std::vector<double>>();
    auto elapsed = std::make_shared<std::vector<double>>();
    auto started = std::make_shared<base::TimeTicks>(base::TimeTicks::Now());
    auto remaining = std::make_shared<int>(iterations);
    for (int i = 0; i < iterations; ++i) {
      *started = base::TimeTicks::Now();
      frame->ExecuteJavaScriptInIsolatedWorld(
          u"document.querySelectorAll('a[href],button').length",
          base::BindOnce(
              [](std::shared_ptr<base::TimeTicks> began,
                 std::shared_ptr<std::vector<double>> samples,
                 std::shared_ptr<int> left, base::Value value) {
                samples->push_back((base::TimeTicks::Now() - *began).InMillisecondsF());
                if (--(*left) > 0) {
                  return;
                }
                std::vector<double> sorted = *samples;
                std::sort(sorted.begin(), sorted.end());
                const double median = sorted[sorted.size() / 2];
                const double p95 = sorted[static_cast<size_t>(sorted.size() * 0.95) - 1];
                LOG(ERROR) << "ENVBENCH: in-browser isolated-world execution n="
                           << sorted.size() << " median=" << median
                           << "ms p95=" << p95 << "ms min=" << sorted.front() << "ms";
              },
              started, state, remaining),
          /*world_id=*/1);
    }
    base::DictValue out;
    out.Set("ok", true);
    out.Set("async", true);
    out.Set("iterations", iterations);
    base::JSONWriter::Write(out, result);
    return;
  }

  if (*command == "activate") {
    const std::string* env_id = d.FindString("env_id");
    if (!env_id) {
      *result = "{\"ok\":false,\"error\":\"missing env_id\"}";
      return;
    }
    // Auto-create on first activation so the CLI does not need a separate step.
    if (!FindEnv(*env_id)) {
      EnvProfile spec;
      spec.env_id = *env_id;
      if (const std::string* v = d.FindString("proxy")) {
        spec.proxy_config = *v;
      }
      if (const std::string* v = d.FindString("expected_exit_ip")) {
        spec.expected_exit_ip = *v;
      }

      if (const std::string* v = d.FindString("locale")) {
        spec.locale = *v;
      }
      if (const std::string* v = d.FindString("accept_languages")) {
        spec.accept_languages = *v;
      }
      if (const std::string* v = d.FindString("display_name")) {
        spec.display_name = *v;
      }
      if (const std::string* v = d.FindString("start_url")) {
        spec.start_url = *v;
      }
      if (const std::string* v = d.FindString("homepage")) {
        spec.homepage = *v;
      }
      if (const std::string* v = d.FindString("group")) {
        spec.group = *v;
      }
      if (const std::string* v = d.FindString("tags")) {
        spec.tags = *v;
      }
      if (const std::string* v = d.FindString("notes")) {
        spec.notes = *v;
      }
      std::string ignored;
      UpsertEnv(spec, &ignored);
    }
    std::string error;
    if (ActivateEnv(*env_id, &error)) {
      // `activate --url` navigates the environment's tab (or, if it has none yet,
      // becomes its start URL). This used to be silently ignored for an existing
      // environment, which made the probes open about:blank.
      if (const std::string* url = d.FindString("start_url")) {
        std::string nav_error;
        if (!NavigateEnv(*env_id, *url, &nav_error)) {
          *result = "{\"ok\":false,\"active\":\"" + *env_id +
                    "\",\"error\":\"" + nav_error + "\"}";
          return;
        }
      }
      *result = "{\"ok\":true,\"active\":\"" + *env_id + "\"}";
    } else {
      *result = "{\"ok\":false,\"error\":\"" + error + "\"}";
    }
    return;
  }

  if (*command == "session") {
    // Open the floating window for a session without clicking the list.
    const std::string* session_id = d.FindString("session_id");
    if (!session_id) {
      *result = "{\"ok\":false,\"error\":\"missing session_id\"}";
      return;
    }
    const std::string title =
        d.FindString("title") ? *d.FindString("title") : std::string();
    if (session_request_handler_) {
      session_request_handler_.Run(*session_id, title);
      *result = "{\"ok\":true,\"session\":\"" + *session_id + "\"}";
    } else {
      *result = "{\"ok\":false,\"error\":\"no panel is listening\"}";
    }
    return;
  }

  if (*command == "tabstrip") {
    // Draw or hide the tab strip. Hiding it also reclaims its height only in the layout sense:
    // the frame band stays, so the window geometry a page can read does not change.
    const bool hide = d.FindBool("hide").value_or(true);
    hide_tab_strip_ = hide;
    ProfileBrowserCollection* collection =
        ProfileBrowserCollection::GetForProfile(profile_);
    if (collection) {
      collection->ForEach(
          [](BrowserWindowInterface* window) {
            if (ui::BaseWindow* base = window->GetWindow()) {
              base->Hide();
              base->Show();
            }
            return true;
          },
          BrowserCollection::Order::kCreation);
    }
    base::DictValue out;
    out.Set("ok", true);
    out.Set("hide_tab_strip", hide_tab_strip_);
    base::JSONWriter::Write(out, result);
    return;
  }

  if (*command == "panel") {
    // `"hide": true` closes the panel instead of showing it (fingerprint experiment).
    const bool hide = d.FindBool("hide").value_or(false);
    if (hide && panel_visibility_handler_) {
      panel_visibility_handler_.Run(/*show=*/false);
      *result = "{\"ok\":true,\"panel\":\"hidden\"}";
      return;
    }
    if (panel_request_handler_) {
      panel_request_handler_.Run();
      *result = "{\"ok\":true,\"panel\":\"shown\"}";
    } else {
      *result = "{\"ok\":false,\"error\":\"no panel in this window\"}";
    }
    return;
  }

  if (*command == "list" || *command == "status") {
    *result = DescribeStateJson();
    return;
  }

  if (*command == "runtime") {
    *result = DescribeRuntimeJson();
    return;
  }

  if (*command == "create") {
    const std::string* env_id = d.FindString("env_id");
    // Field-by-field update: `create` on an existing environment must not wipe the
    // fields the command did not mention. Measured cost of the previous wholesale
    // replacement: `create fb-008 --url ...` cleared that environment's proxy, seed
    // and expected exit, so it silently went direct.
    EnvProfile* existing = env_id ? FindMutableEnv(*env_id) : nullptr;
    EnvProfile spec = existing ? *existing : EnvProfile();
    if (env_id) {
      spec.env_id = *env_id;
    }
    if (const std::string* v = d.FindString("proxy")) {
      spec.proxy_config = *v;
    }
    if (const std::string* v = d.FindString("expected_exit_ip")) {
      spec.expected_exit_ip = *v;
    }
    if (const std::string* v = d.FindString("ip_mode")) {
      spec.ip_mode = *v == "dynamic" ? "dynamic" : "static";
    }
    if (const std::string* v = d.FindString("locale")) {
      spec.locale = *v;
    }
    if (const std::string* v = d.FindString("accept_languages")) {
      spec.accept_languages = *v;
    }
    if (const std::string* v = d.FindString("display_name")) {
      spec.display_name = *v;
    }
    if (const std::string* v = d.FindString("group")) {
      spec.group = *v;
    }
    if (const std::string* v = d.FindString("tags")) {
      spec.tags = *v;
    }
    if (const std::string* v = d.FindString("notes")) {
      spec.notes = *v;
    }
    if (const std::string* v = d.FindString("fingerprint_seed")) {
      spec.fingerprint_seed = *v;
    }
    if (const std::string* v = d.FindString("start_url")) {
      spec.start_url = *v;
    }
    if (const std::string* v = d.FindString("homepage")) {
      spec.homepage = *v;
    }
    std::string error;
    if (UpsertEnv(spec, &error)) {
      *result = "{\"ok\":true}";
    } else {
      *result = "{\"ok\":false,\"error\":\"" + error + "\"}";
    }
    return;
  }

  if (*command == "delete") {
    const std::string* env_id = d.FindString("env_id");
    std::string error;
    if (!env_id || !DeleteEnv(*env_id, &error)) {
      base::DictValue out;
      out.Set("ok", false);
      out.Set("error", env_id ? error : "missing env_id");
      base::JSONWriter::Write(out, result);
    } else {
      *result = "{\"ok\":true,\"storage_preserved\":true}";
    }
    return;
  }

  if (*command == "selecttab") {
    const int index = d.FindInt("index").value_or(-1);
    const std::string* env_id = d.FindString("env_id");
    ProfileBrowserCollection* collection =
        ProfileBrowserCollection::GetForProfile(profile_);
    BrowserWindowInterface* window =
        collection ? collection->GetLastActiveBrowser() : nullptr;
    TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
    if (!strip || index < 0 || index >= strip->count()) {
      *result = "{\"ok\":false,\"error\":\"bad index or no strip\"}";
      return;
    }
    if (env_id && EnvIdOf(strip->GetWebContentsAt(index)) != *env_id) {
      *result = "{\"ok\":false,\"error\":\"that tab belongs to another environment\"}";
      return;
    }
    strip->ActivateTabAt(index);
    base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvManager::FocusActivePage,
                                weak_factory_.GetWeakPtr()));
    base::DictValue out;
    out.Set("ok", true);
    out.Set("active_index", strip->active_index());
    base::JSONWriter::Write(out, result);
    return;
  }

  if (*command == "overlay") {
    ProfileBrowserCollection* overlay_collection =
        ProfileBrowserCollection::GetForProfile(profile_);
    // Authorization owns a separate foreground window. Bridge hide/show/query
    // must still address the existing assistant, also before any environment
    // has been created; the last active browser may be that login popup.
    BrowserWindowInterface* window =
        MainWindowIfReady();
    if (!window && overlay_collection) {
      overlay_collection->ForEach([&](BrowserWindowInterface* candidate) {
        if (env::OverlayGeometry(candidate) != "none") {
          window = candidate;
          return false;
        }
        return true;
      });
    }
    if (!window && overlay_collection) {
      window = overlay_collection->GetLastActiveBrowser();
    }
    if (!window) {
      *result = R"({"ok":false,"error":"no window"})";
      return;
    }
    if (d.FindBool("query").value_or(false)) {
      base::DictValue out;
      out.Set("ok", true);
      out.Set("visible", env::OverlayVisible(window));
      out.Set("geometry", env::OverlayGeometry(window));
      base::JSONWriter::Write(out, result);
      return;
    }
    const bool on = d.FindBool("on").value_or(true);
    std::string mode;
    if (const std::string* v = d.FindString("mode")) {
      mode = *v;
    }
    if (mode == "refresh") {
      const std::string* requested = d.FindString("url");
      const GURL candidate(requested ? *requested : "");
      const bool refreshed = candidate.is_valid() && env::RefreshOverlay(window, candidate);
      base::DictValue out;
      out.Set("ok", candidate.is_valid());
      out.Set("refreshed", refreshed);
      out.Set("visible", env::OverlayVisible(window));
      base::JSONWriter::Write(out, result);
      return;
    }
    // A URL in the payload re-points the layer first: the workbench picks a new port on every restart, and a mode request
    // that ignored it left the layer showing the dead one.
    if (const std::string* requested = d.FindString("url")) {
      if (GURL candidate(*requested); candidate.is_valid()) {
        std::string ignored;
        env::ShowOverlay(window, candidate, &ignored);
      }
    }
    if (mode == "rail") {
      env::CollapseOverlayToRail(window);
      base::DictValue out;
      out.Set("ok", true);
      out.Set("mode", "rail");
      out.Set("geometry", env::OverlayGeometry(window));
      base::JSONWriter::Write(out, result);
      return;
    }
    if (mode == "expand") {
      env::ExpandOverlayFromRail(window);
      base::DictValue out;
      out.Set("ok", true);
      out.Set("mode", "expand");
      out.Set("geometry", env::OverlayGeometry(window));
      base::JSONWriter::Write(out, result);
      return;
    }
    if (!on) {
      if (mode == "shrink") {
        env::ShrinkOverlay(window);
      } else {
        env::HideOverlay(window);
      }
    } else {
      // Showing an existing layer only needs to raise it; a url is required only to create one.
      env::RestoreOverlay(window);
      std::string url_text;
      if (const std::string* v = d.FindString("url")) {
        url_text = *v;
      }
      if (url_text.empty() && env::OverlayVisible(window)) {
        base::DictValue out;
        out.Set("ok", true);
        out.Set("visible", true);
        out.Set("geometry", env::OverlayGeometry(window));
        base::JSONWriter::Write(out, result);
        return;
      }
      GURL url(url_text);
      if (!url.is_valid()) {
        *result = R"({"ok":false,"error":"bad url"})";
        return;
      }
      std::string error;
      if (!env::ShowOverlay(window, url, &error)) {
        base::DictValue out;
        out.Set("ok", false);
        out.Set("error", error);
        base::JSONWriter::Write(out, result);
        return;
      }
    }
    if (std::optional<int> width = d.FindInt("width"); width && *width > 0) {
      env::SetOverlayWidth(window, *width);
    }
    base::DictValue out;
    out.Set("ok", true);
    out.Set("visible", env::OverlayVisible(window));
    out.Set("geometry", env::OverlayGeometry(window));
    base::JSONWriter::Write(out, result);
    LOG(ERROR) << "ENVOVERLAY: command on=" << on << " " << env::OverlayGeometry(window);
    return;
  }

  if (*command == "windows") {
    ProfileBrowserCollection* collection =
        ProfileBrowserCollection::GetForProfile(profile_);
    base::ListValue windows;
    if (collection) {
      collection->ForEach(
          [&](BrowserWindowInterface* window) {
            TabStripModel* strip = window->GetTabStripModel();
            if (!strip) {
              return true;
            }
            base::DictValue entry;
            ui::BaseWindow* base = window->GetWindow();
            entry.Set("visible", base && base->IsVisible());
            if (base) {
              entry.Set("bounds", base->GetBounds().ToString());
              entry.Set("active_window", base->IsActive());
            }
            base::ListValue tabs;
            base::ListValue envs;
            for (int i = 0; i < strip->count(); ++i) {
              content::WebContents* contents = strip->GetWebContentsAt(i);
              if (!contents) {
                continue;
              }
              base::DictValue tab;
              const std::string env = EnvIdOf(contents);
              tab.Set("env", env);
              tab.Set("url", contents->GetVisibleURL().spec().substr(0, 70));
              tab.Set("title", base::UTF16ToUTF8(contents->GetTitle()).substr(0, 40));
              tab.Set("loading", contents->IsLoading());
              tabs.Append(std::move(tab));
              if (!env.empty()) {
                envs.Append(env);
              }
            }
            entry.Set("tabs", std::move(tabs));
            entry.Set("envs", std::move(envs));
            windows.Append(std::move(entry));
            return true;
          },
          BrowserCollection::Order::kCreation);
    }
    base::DictValue out;
    out.Set("ok", true);
    out.Set("active", active_env_id_);
    out.Set("windows", std::move(windows));
    base::JSONWriter::Write(out, result);
    return;
  }

  if (*command == "tabgeometry") {
    base::DictValue snapshot = GetTabStripGeometry();
    snapshot.Set("ok", true);
    base::JSONWriter::Write(snapshot, result);
    return;
  }

  if (*command == "strip") {
    // What the tab strip actually contains, in order, plus which entry is selected. Written for
    // the switch bugs: a screenshot cannot tell "the tab came back in the wrong position" from
    // "the same tab is showing".
    ProfileBrowserCollection* collection =
        ProfileBrowserCollection::GetForProfile(profile_);
    BrowserWindowInterface* window =
        collection ? collection->GetLastActiveBrowser() : nullptr;
    TabStripModel* strip = window ? window->GetTabStripModel() : nullptr;
    if (!strip) {
      *result = "{\"ok\":false,\"error\":\"no tab strip\"}";
      return;
    }
    base::ListValue tabs;
    for (int i = 0; i < strip->count(); ++i) {
      content::WebContents* contents = strip->GetWebContentsAt(i);
      base::DictValue tab;
      tab.Set("index", i);
      tab.Set("title", contents ? base::UTF16ToUTF8(contents->GetTitle())
                                : std::string());
      tab.Set("url", contents ? contents->GetVisibleURL().spec() : std::string());
      tab.Set("env", contents ? EnvIdOf(contents) : std::string());
      tab.Set("active", i == strip->active_index());
      tabs.Append(std::move(tab));
    }
    base::DictValue out;
    out.Set("ok", true);
    out.Set("count", strip->count());
    out.Set("active_index", strip->active_index());
    out.Set("tabs", std::move(tabs));
    base::JSONWriter::Write(out, result);
    return;
  }

  if (*command == "newtab") {
    const std::string* env_id = d.FindString("env_id");
    if (!env_id) {
      *result = "{\"ok\":false,\"error\":\"missing env_id\"}";
      return;
    }
    const std::string* url = d.FindString("url");
    std::string error;
    if (NewTab(*env_id, url ? *url : std::string(), &error)) {
      *result = "{\"ok\":true}";
    } else {
      *result = "{\"ok\":false,\"error\":\"" + error + "\"}";
    }
    return;
  }

  *result = "{\"ok\":false,\"error\":\"unknown command: " + *command + "\"}";
}

}  // namespace env
