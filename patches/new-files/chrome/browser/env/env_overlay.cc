// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_overlay.h"

#include <algorithm>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "base/environment.h"
#include "base/files/file_path.h"
#include "base/files/file_util.h"
#include "base/json/json_reader.h"
#include "base/memory/raw_ptr.h"
#include "base/memory/weak_ptr.h"
#include "base/functional/bind.h"
#include "base/task/single_thread_task_runner.h"
#include "base/strings/utf_string_conversions.h"
#include "base/no_destructor.h"
#include "base/path_service.h"
#include "base/strings/string_number_conversions.h"
#include "chrome/browser/profiles/profile.h"
#include "chrome/browser/ui/views/frame/browser_view.h"
#include "content/public/browser/navigation_controller.h"
#include "content/public/browser/render_frame_host.h"
#include "content/public/browser/web_contents.h"
#include "content/public/browser/web_contents_observer.h"
#include "url/origin.h"
#include "third_party/skia/include/core/SkPath.h"
#include "third_party/skia/include/core/SkPathBuilder.h"
#include "ui/aura/window.h"
#include "ui/base/metadata/metadata_header_macros.h"
#include "ui/base/metadata/metadata_impl_macros.h"
#include "ui/base/page_transition_types.h"
#include "ui/events/event.h"
#include "ui/events/event_handler.h"
#include "ui/gfx/canvas.h"
#include "ui/gfx/geometry/rect.h"
#include "ui/gfx/geometry/rounded_corners_f.h"
#include "ui/gfx/geometry/skia_conversions.h"
#include "ui/views/controls/native/native_view_host.h"
#include "ui/views/controls/button/md_text_button.h"
#include "ui/views/layout/box_layout.h"
#include "chrome/browser/env/env_manager.h"
#include "ui/views/controls/webview/webview.h"
#include "ui/views/view.h"
#include "ui/views/view_observer.h"
#include "ui/views/view_targeter.h"
#include "ui/views/view_targeter_delegate.h"
#include "ui/views/widget/widget.h"
#include "url/gurl.h"

namespace env {

// The dock meets the left, top and bottom content edges. Only the conversation
// extends above the page; its outer right corners retain their rounding.
constexpr int kMargin = 0;
constexpr int kColumnGap = 12;
// The embedded DSH sidebar folds into an icon rail below this width (measured: collapsed at 960, expanded at
// 1024), and the sidebar seats live in the expanded layout, so the layer never gets narrower than this.
constexpr int kMinLayerWidth = 1024;
// Keep the rendering viewport wide while the native clip follows the sidebar.
constexpr SkScalar kCornerRadius = 16;

class EnvOverlayHost;
class EnvOverlayClickAway;

namespace {

using HostMap = std::map<BrowserView*, EnvOverlayHost*>;

HostMap& Hosts() {
  static base::NoDestructor<HostMap> hosts;
  return *hosts;
}

}  // namespace

// A layer above the page, parented to the BrowserView rather than to the contents container.
//
// Measured reason: as a child of `BrowserView::contents_container()` (a MultiContentsView) the host was laid
// out to 0x0 and later hidden by that view's layout while its WebContents kept painting - the layer looked
// visible but `GetVisible()` said otherwise, so the toolbar toggle always took the "show" branch. A BrowserView
// child is not managed by that layout, so the layer owns its bounds and visibility here.
class EnvOverlayHost : public views::View,
                       public views::ViewObserver,
                       public views::ViewTargeterDelegate,
                       public content::WebContentsObserver {
  METADATA_HEADER(EnvOverlayHost, views::View)

 public:
  EnvOverlayHost(content::BrowserContext* context,
                 const GURL& url,
                 BrowserView* browser_view,
                 views::View* content_area);
  EnvOverlayHost(const EnvOverlayHost&) = delete;
  EnvOverlayHost& operator=(const EnvOverlayHost&) = delete;
  ~EnvOverlayHost() override;

  // views::View:
  void Layout(PassKey) override { LayoutContents(); }

  // The layer paints its own card: the title bar sits on the same rounded rect as the WebView, so the page underneath
  // cannot bleed through the bar area (measured: the page's IP text showed through and read as junk next to 收起).
  void OnPaint(gfx::Canvas* canvas) override;

  // views::ViewObserver:
  void OnViewBoundsChanged(views::View* observed_view) override;

  void InstallClickAway(aura::Window* event_window);
  void SetPageWindow(aura::Window* page_window) { page_window_ = page_window; }

  // Re-resolve the browsing page's native window; safe to call whenever it may have changed.
  void RefreshPageWindow() {
    if (!browser_view_) {
      return;
    }
    if (views::WebView* contents = browser_view_->GetActiveContentsWebView()) {
      if (contents->holder()) {
        page_window_ = contents->holder()->native_view();
      }
    }
  }

  bool IsShrunk() const { return shrunk_; }


  // A restarted workbench has a new port and token, so an existing layer has to follow the new URL instead of
  // keeping a dead page (measured: after a workbench restart the layer showed a blank page).
  void NavigateTo(const GURL& url) {
    if (url_ == url || !web_view_ || !web_view_->GetWebContents()) {
      return;
    }
    url_ = url;
    web_view_->GetWebContents()->GetController().LoadURL(
        url, content::Referrer(), ui::PAGE_TRANSITION_AUTO_TOPLEVEL, std::string());
    LOG(ERROR) << "ENVOVERLAY: navigated to " << url.spec().substr(0, 70);
  }
  bool IsLayerShown() const { return web_view_ && web_view_->GetVisible() && !shrunk_; }

  void SetOverlayVisible(bool visible);
  gfx::Rect ReserveContentBounds(const gfx::Rect& bounds);
  // Width override: 0 keeps the product 2/3 rule. The DSH web client folds its sidebar into an icon rail below
  // its own breakpoint, so the sidebar seats are only reachable above that width.
  void SetWidthOverride(int width) {
    width_override_ = width;
    LayoutContents();
  }

  void SetShrunk(bool shrunk);
  // Clicking the page no longer hides the layer: it folds back to the session rail, and whatever is running keeps
  // running (measured requirement: "clicking blank space makes it disappear but the task continues").
  // No browser-to-renderer JavaScript is needed to update the native clip.
  void WriteLayerStateFile();
  void CollapseToRail();
  void ExpandFromRail();

  bool PressLandedOnLayer(const ui::MouseEvent& event) const;
  bool PressLandedOnPage(const ui::MouseEvent& event) const;

  std::string Describe() const;

 private:
  void LayoutContents();
  gfx::Rect ContentAreaInParent() const;
  gfx::Rect VisibleCardBounds() const;
  void ApplyPageAction(std::string action, int sidebar_width);

  bool DoesIntersectRect(const views::View* target,
                         const gfx::Rect& rect) const override {
    return VisibleCardBounds().Intersects(rect);
  }

  void OnDidAddMessageToConsole(
      content::RenderFrameHost* source_frame,
      blink::mojom::ConsoleMessageLevel log_level,
      const std::u16string& message,
      int32_t line_no,
      const std::u16string& source_id,
      const std::optional<std::u16string>& untrusted_stack_trace) override;

 public:
  bool AllowsNativeEvent(gfx::NativeView child,
                         const gfx::Point& root_point) const;

 private:

  raw_ptr<BrowserView> browser_view_ = nullptr;
  raw_ptr<views::View> content_area_ = nullptr;
  raw_ptr<views::WebView> web_view_ = nullptr;
  // The layer owns its environment switcher: a plain view hierarchy above the WebView, so the DSH sidebar is not
  // involved and the old native panel stays retired.
  raw_ptr<EnvManager> manager_ = nullptr;
  std::string active_env_id_;
  raw_ptr<aura::Window> event_window_ = nullptr;
  raw_ptr<aura::Window> page_window_ = nullptr;
  std::unique_ptr<EnvOverlayClickAway> click_away_;
  bool shrunk_ = false;
  bool collapsed_ = true;
  int sidebar_width_ = 280;
  int reserved_width_ = 0;
  gfx::Rect full_content_bounds_;
  int width_override_ = 0;
  GURL url_;
  base::WeakPtrFactory<EnvOverlayHost> weak_factory_{this};
};

// A mouse press outside the layer, on the page, hides it. Registered as a pre-target handler on the browser
// window so it observes the press before any view handles it: the event is never consumed and the page
// underneath still receives its click (measured: mousedown counter 0 -> 1 with the layer visible).
class EnvOverlayClickAway : public ui::EventHandler {
 public:
  explicit EnvOverlayClickAway(EnvOverlayHost* host) : host_(host) {}

  void OnMouseEvent(ui::MouseEvent* event) override {
    if (event->type() != ui::EventType::kMousePressed || !host_) {
      return;
    }
    if (!host_->IsLayerShown()) {
      return;
    }
    if (host_->PressLandedOnLayer(*event)) {
      return;
    }
    if (!host_->PressLandedOnPage(*event)) {
      return;
    }
    // The same press continues to the browsing page after the conversation is clipped away.
    host_->CollapseToRail();
  }

 private:
  raw_ptr<EnvOverlayHost> host_ = nullptr;
};

EnvOverlayHost::EnvOverlayHost(content::BrowserContext* context,
                               const GURL& url,
                               BrowserView* browser_view,
                               views::View* content_area)
    : browser_view_(browser_view), content_area_(content_area) {
  SetCanProcessEventsWithinSubtree(true);
  SetEventTargeter(std::make_unique<views::ViewTargeter>(this));
  if (browser_view_) {
    manager_ = EnvManager::GetForProfile(browser_view_->GetProfile());
    if (manager_) {
      active_env_id_ = manager_->active_env_id();
    }
  }

  // No C++ title bar: it left an unpainted strip where the page showed through. The page draws its own collapse button
  // and asks the bridge to hide the layer; the layer itself is just the rounded card.
  web_view_ = AddChildView(std::make_unique<views::WebView>(context));

  auto contents = content::WebContents::Create(content::WebContents::CreateParams(context));
  contents->GetController().LoadURL(url, content::Referrer(),
                                    ui::PAGE_TRANSITION_AUTO_TOPLEVEL, std::string());
  // Opaque base: the workbench relies on it to paint the sidebar's surface. Making it transparent (tried for the freed
  // collapse area) also made the sidebar show the page through it, which is worse than an empty white space.
  contents->SetPageBaseBackgroundColor(SK_ColorWHITE);
  web_view_->SetOwnedWebContents(std::move(contents));
  Observe(web_view_->GetWebContents());
  url_ = url;

  browser_view_->AddObserver(this);
  if (content_area_) {
    content_area_->AddObserver(this);
  }
  SetBoundsRect(ContentAreaInParent());
  LayoutContents();
  LOG(ERROR) << "ENVOVERLAY: attached url=" << url.spec().substr(0, 70);
}

EnvOverlayHost::~EnvOverlayHost() {
  Observe(nullptr);
  if (event_window_ && click_away_) {
    event_window_->RemovePreTargetHandler(click_away_.get());
  }
  if (content_area_) {
    content_area_->RemoveObserver(this);
  }
  if (browser_view_) {
    browser_view_->RemoveObserver(this);
  }
  auto it = Hosts().find(browser_view_);
  if (it != Hosts().end() && it->second == this) {
    Hosts().erase(it);
  }
}

void EnvOverlayHost::OnViewBoundsChanged(views::View* observed_view) {
  if (observed_view != browser_view_ && observed_view != content_area_) {
    return;
  }
  LayoutContents();
}

gfx::Rect EnvOverlayHost::ContentAreaInParent() const {
  if (!browser_view_ || !content_area_) {
    return gfx::Rect();
  }
  // The browser layout gives us the area before reserving the sidebar. Do not
  // anchor to the inset page bounds or the host would shift with the page.
  if (!full_content_bounds_.IsEmpty()) {
    return full_content_bounds_;
  }
  gfx::Rect bounds = content_area_->bounds();
  if (bounds.IsEmpty()) {
    bounds = browser_view_->GetLocalBounds();
  }
  return bounds;
}

gfx::Rect EnvOverlayHost::ReserveContentBounds(const gfx::Rect& bounds) {
  const bool area_changed = full_content_bounds_ != bounds;
  full_content_bounds_ = bounds;
  // Leave the browsing page usable at the browser's smallest supported size.
  reserved_width_ = IsLayerShown()
      ? std::min(sidebar_width_ + kMargin * 2, std::max(0, bounds.width() - 200))
      : 0;
  if (area_changed) {
    LayoutContents();
  }
  gfx::Rect page = bounds;
  page.Inset(gfx::Insets::TLBR(0, reserved_width_, 0, 0));
  return page;
}

gfx::Rect EnvOverlayHost::VisibleCardBounds() const {
  if (!web_view_) {
    return gfx::Rect();
  }
  gfx::Rect card = web_view_->bounds();
  if (collapsed_) {
    card.set_width(std::min(sidebar_width_, card.width()));
  }
  return card;
}

void EnvOverlayHost::LayoutContents() {
  if (!web_view_) {
    return;
  }
  const gfx::Rect content = ContentAreaInParent();
  if (content.IsEmpty()) {
    return;
  }
  const int available = std::max(1, content.width() - kMargin * 2);
  const int viewport_width = std::min(
      available, std::max(kMinLayerWidth, width_override_ > 0
          ? width_override_ : content.width() * 2 / 3 - kMargin * 2 - kColumnGap));
  const int height = std::max(1, content.height() - kMargin * 2);
  const int visible_width = collapsed_ ? std::min(sidebar_width_, viewport_width)
                                     : viewport_width;
  // The viewport stays wide so DSH retains its own full sidebar. Only the
  // containing View and native surface clip shrink; no page scaling or CSS hide.
  SetBoundsRect(gfx::Rect(content.x(), content.y(),
                         (shrunk_ ? 2 : visible_width) + kMargin * 2,
                         content.height()));
  web_view_->SetBounds(kMargin, kMargin, shrunk_ ? 2 : viewport_width,
                       shrunk_ ? 2 : height);
  web_view_->holder()->SetNativeViewClipRect(
      gfx::Rect(0, 0, shrunk_ ? 2 : visible_width, shrunk_ ? 2 : height));
  web_view_->holder()->SetNativeViewCornerRadii(
      collapsed_ ? gfx::RoundedCornersF()
                 : gfx::RoundedCornersF(0, kCornerRadius, kCornerRadius, 0));
  web_view_->holder()->InvalidateLayout();
}

void EnvOverlayHost::OnPaint(gfx::Canvas* canvas) {
  // Paint only the visible card. A full-width backing card would cover the page
  // even after clipping its native child.
  if (!web_view_ || shrunk_) {
    return;
  }
  SkPathBuilder builder;
  builder.addRRect(gfx::RoundedRectToSkRRect(
      VisibleCardBounds(),
      collapsed_ ? gfx::RoundedCornersF()
                 : gfx::RoundedCornersF(0, kCornerRadius, kCornerRadius, 0)));
  cc::PaintFlags flags;
  flags.setAntiAlias(true);
  flags.setStyle(cc::PaintFlags::kFill_Style);
  flags.setColor(GetColorProvider()->GetColor(kColorToolbar));
  canvas->DrawPath(builder.detach(), flags);
}

bool EnvOverlayHost::AllowsNativeEvent(gfx::NativeView child,
                                      const gfx::Point& root_point) const {
  if (!IsLayerShown() || !web_view_->holder() || !GetWidget()) {
    return true;
  }
  aura::Window* container = web_view_->holder()->GetNativeViewContainer();
  if (!container || !(child == container || container->Contains(child))) {
    return true;
  }
  gfx::Point point(root_point);
  views::View::ConvertPointToTarget(GetWidget()->GetRootView(), this, &point);
  return VisibleCardBounds().Contains(point);
}

void EnvOverlayHost::OnDidAddMessageToConsole(
    content::RenderFrameHost* source_frame,
    blink::mojom::ConsoleMessageLevel log_level,
    const std::u16string& message,
    int32_t line_no,
    const std::u16string& source_id,
    const std::optional<std::u16string>& untrusted_stack_trace) {
  // One-way UI notifications from this embedded workbench only. No file-channel
  // race, repeated HTTP poll, navigation, or script execution in the renderer.
  if (!web_contents() || source_frame != web_contents()->GetPrimaryMainFrame() ||
      source_frame->GetLastCommittedOrigin() != url::Origin::Create(url_)) {
    return;
  }
  const std::string text = base::UTF16ToUTF8(message);
  constexpr char prefix[] = "LAN_OVERLAY_UI:";
  if (!text.starts_with(prefix) || text.size() > 1024) {
    return;
  }
  auto parsed = base::JSONReader::Read(text.substr(sizeof(prefix) - 1),
                                       base::JSON_PARSE_RFC);
  if (!parsed || !parsed->is_dict()) {
    return;
  }
  const auto& value = parsed->GetDict();
  const std::string* action = value.FindString("action");
  if (!action || (*action != "ready" && *action != "collapse" && *action != "expand")) {
    return;
  }
  const int sidebar_width = value.FindInt("sidebar_width").value_or(0);
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvOverlayHost::ApplyPageAction,
                               weak_factory_.GetWeakPtr(), *action, sidebar_width));
}

void EnvOverlayHost::ApplyPageAction(std::string action, int sidebar_width) {
  // DSH's own collapsed sidebar is 56 DIP; both sidebar states reserve space.
  if (sidebar_width >= 32 && sidebar_width <= 480 &&
      sidebar_width_ != sidebar_width) {
    sidebar_width_ = sidebar_width;
    browser_view_->InvalidateLayout();
  }
  if (action == "collapse") {
    CollapseToRail();
  } else if (action == "expand") {
    ExpandFromRail();
  } else {
    LayoutContents();
    SchedulePaint();
  }
}

void EnvOverlayHost::SetOverlayVisible(bool visible) {
  const bool changed = GetVisible() != visible ||
                       (web_view_ && web_view_->GetVisible() != visible);
  SetVisible(visible);
  if (web_view_) {
    web_view_->SetVisible(visible);
  }
  // The page reads its state from this file; without the write on hide/show it kept seeing the old value and retried forever.
  WriteLayerStateFile();
  if (changed && browser_view_) {
    browser_view_->InvalidateLayout();
  }
  LOG(ERROR) << "ENVOVERLAY: visible=" << visible << " " << Describe();
}

void EnvOverlayHost::SetShrunk(bool shrunk) {
  const bool changed = shrunk_ != shrunk;
  shrunk_ = shrunk;
  LayoutContents();
  SetVisible(true);
  if (web_view_) {
    web_view_->SetVisible(true);
  }
  if (changed && browser_view_) {
    browser_view_->InvalidateLayout();
  }
}

void EnvOverlayHost::WriteLayerStateFile() {
  Profile* profile = browser_view_ ? browser_view_->GetProfile() : nullptr;
  if (!profile) {
    return;
  }
  const std::string json = std::string("{\"collapsed\": ") + (collapsed_ ? "true" : "false") +
                           ", \"visible\": " + (IsLayerShown() ? "true" : "false") + "}";
  base::FilePath path = profile->GetPath().AppendASCII("lan-layer-state.json");
  base::WriteFile(path, json);
}

void EnvOverlayHost::CollapseToRail() {
  collapsed_ = true;
  WriteLayerStateFile();
  LayoutContents();               // the clip changes here, in the same frame as the click
  SchedulePaint();
  SetOverlayVisible(true);                // stays on screen: the sidebar must stay visible
  LOG(ERROR) << "ENVOVERLAY: conversation collapsed, " << Describe();
}

void EnvOverlayHost::ExpandFromRail() {
  collapsed_ = false;
  WriteLayerStateFile();
  LayoutContents();
  SchedulePaint();
  SetOverlayVisible(true);
  LOG(ERROR) << "ENVOVERLAY: conversation expanded, " << Describe();
}

bool EnvOverlayHost::PressLandedOnLayer(const ui::MouseEvent& event) const {
  aura::Window* target = static_cast<aura::Window*>(event.target());
  aura::Window* layer_window =
      (web_view_ && web_view_->holder()) ? web_view_->holder()->native_view() : nullptr;
  return target && layer_window && (target == layer_window || layer_window->Contains(target));
}

bool EnvOverlayHost::PressLandedOnPage(const ui::MouseEvent& event) const {
  aura::Window* target = static_cast<aura::Window*>(event.target());
  if (!target) {
    return false;
  }
  // Chrome presses target the frame window: a different pointer from the widget window but the same screen
  // rect (measured: both 331,106 1386x894), so geometry identifies it.
  if (event_window_ && target->GetBoundsInScreen() == event_window_->GetBoundsInScreen()) {
    return false;
  }
  // Environment activation can replace the tab and its native view.
  const_cast<EnvOverlayHost*>(this)->RefreshPageWindow();
  return page_window_ && (target == page_window_ || page_window_->Contains(target));
}

std::string EnvOverlayHost::Describe() const {
  std::string text = "host=" + bounds().ToString() +
                     " visible=" + base::NumberToString(GetVisible() ? 1 : 0) +
                     " layer=" + base::NumberToString(IsLayerShown() ? 1 : 0) +
                     " collapsed=" + base::NumberToString(collapsed_ ? 1 : 0);
  if (web_view_) {
    text += " webview=" + web_view_->bounds().ToString();
    text += " card=" + VisibleCardBounds().ToString();
    text += " native_clip=" + web_view_->holder()->GetNativeViewClipRect().ToString();
  }
  text += " reserved=" + base::NumberToString(reserved_width_);
  if (content_area_) {
    text += " page=" + content_area_->bounds().ToString();
  }
  text += " area=" + full_content_bounds_.ToString();
  return text;
}

void EnvOverlayHost::InstallClickAway(aura::Window* event_window) {
  if (!event_window) {
    return;
  }
  event_window_ = event_window;
  click_away_ = std::make_unique<EnvOverlayClickAway>(this);
  event_window_->AddPreTargetHandler(click_away_.get());
  LOG(ERROR) << "ENVOVERLAY: click-away installed on "
             << event_window_->GetBoundsInScreen().ToString();
}

namespace {

EnvOverlayHost* Find(BrowserWindowInterface* window) {
  BrowserView* browser_view = BrowserView::GetBrowserViewForBrowser(window);
  if (!browser_view) {
    return nullptr;
  }
  auto it = Hosts().find(browser_view);
  return it == Hosts().end() ? nullptr : it->second;
}

// Reads the workbench state file: `LAN_PRODUCT_HOME` first (exported by the launcher), then up to six ancestors
// of the executable directory.
GURL WorkbenchUrlFromStateFile() {
  std::vector<base::FilePath> candidates;
  if (std::optional<std::string> home =
          base::Environment::Create()->GetVar("LAN_PRODUCT_HOME")) {
    candidates.push_back(base::FilePath::FromUTF8Unsafe(*home).AppendASCII(".dsh-web.json"));
  }
  base::FilePath exe_dir;
  if (base::PathService::Get(base::DIR_EXE, &exe_dir)) {
    base::FilePath dir = exe_dir;
    for (int depth = 0; depth < 6 && !dir.empty(); ++depth) {
      candidates.push_back(dir.AppendASCII(".dsh-web.json"));
      dir = dir.DirName();
    }
  }
  for (const base::FilePath& state : candidates) {
    std::string contents;
    if (!base::ReadFileToString(state, &contents)) {
      continue;
    }
    std::optional<base::Value> parsed = base::JSONReader::Read(contents, base::JSON_PARSE_RFC);
    if (!parsed || !parsed->is_dict()) {
      continue;
    }
    if (const std::string* url = parsed->GetDict().FindString("url")) {
      LOG(ERROR) << "ENVOVERLAY: workbench url from " << state.value();
      return GURL(*url);
    }
  }
  return GURL();
}

}  // namespace

GURL WorkbenchUrlForProduct() {
  return WorkbenchUrlFromStateFile();
}

bool OverlayAllowsNativeEvent(BrowserView* browser_view,
                              gfx::NativeView child,
                              const gfx::Point& root_point) {
  auto it = Hosts().find(browser_view);
  return it == Hosts().end() || it->second->AllowsNativeEvent(child, root_point);
}

gfx::Rect ReserveAssistantSidebar(views::View* browser_view,
                                  const gfx::Rect& content_bounds) {
  for (const auto& [view, host] : Hosts()) {
    if (view == browser_view) {
      return host->ReserveContentBounds(content_bounds);
    }
  }
  return content_bounds;
}

bool ShowOverlay(BrowserWindowInterface* window, const GURL& url, std::string* error) {
  BrowserView* browser_view = BrowserView::GetBrowserViewForBrowser(window);
  if (!browser_view) {
    *error = "no browser view";
    return false;
  }
  if (EnvOverlayHost* existing = Find(window)) {
    existing->NavigateTo(url);
    existing->SetOverlayVisible(true);
    LOG(ERROR) << "ENVOVERLAY: reused existing host " << existing->Describe();
    return true;
  }
  views::View* content_area = browser_view->contents_container();
  auto host = std::make_unique<EnvOverlayHost>(browser_view->GetProfile(), url, browser_view,
                                               content_area);
  EnvOverlayHost* raw = browser_view->AddChildView(std::move(host));
  Hosts()[browser_view] = raw;

  if (views::Widget* widget = browser_view->GetWidget()) {
    raw->InstallClickAway(widget->GetNativeWindow());
  }
  if (views::WebView* contents_view = browser_view->GetActiveContentsWebView()) {
    if (contents_view->holder()) {
      raw->SetPageWindow(contents_view->holder()->native_view());
    }
  }
  raw->SetOverlayVisible(true);
  return true;
}

bool HideOverlay(BrowserWindowInterface* window) {
  if (EnvOverlayHost* host = Find(window)) {
    host->SetOverlayVisible(false);
    return true;
  }
  return false;
}

bool SetOverlayWidth(BrowserWindowInterface* window, int width) {
  if (EnvOverlayHost* host = Find(window)) {
    host->SetWidthOverride(width);
    LOG(ERROR) << "ENVOVERLAY: width override " << width << " " << host->Describe();
    return true;
  }
  return false;
}

bool CollapseOverlayToRail(BrowserWindowInterface* window) {
  if (EnvOverlayHost* host = Find(window)) {
    host->CollapseToRail();
    return true;
  }
  return false;
}

bool ExpandOverlayFromRail(BrowserWindowInterface* window) {
  if (EnvOverlayHost* host = Find(window)) {
    host->ExpandFromRail();
    return true;
  }
  return false;
}

bool ShrinkOverlay(BrowserWindowInterface* window) {
  if (EnvOverlayHost* host = Find(window)) {
    host->SetShrunk(true);
    return true;
  }
  return false;
}

bool RestoreOverlay(BrowserWindowInterface* window) {
  if (EnvOverlayHost* host = Find(window)) {
    host->SetShrunk(false);
    host->SetOverlayVisible(true);
    return true;
  }
  return false;
}

bool OverlayVisible(BrowserWindowInterface* window) {
  EnvOverlayHost* host = Find(window);
  return host && host->IsLayerShown();
}

std::string OverlayGeometry(BrowserWindowInterface* window) {
  EnvOverlayHost* host = Find(window);
  return host ? host->Describe() : "none";
}

void ShowAssistantLayer(BrowserWindowInterface* window) {
  if (!window || OverlayVisible(window)) {
    return;
  }
  ToggleAssistantLayer(window);
}

void ToggleAssistantLayer(BrowserWindowInterface* window) {
  if (!window) {
    return;
  }
  LOG(ERROR) << "ENVOVERLAY: toggle: host=" << (Find(window) ? "found" : "null")
             << " visible=" << OverlayVisible(window);
  if (OverlayVisible(window)) {
    HideOverlay(window);
    return;
  }
  if (const GURL url = WorkbenchUrlFromStateFile(); url.is_valid()) {
    std::string error;
    ShowOverlay(window, url, &error);
    return;
  }
  LOG(ERROR) << "ENVOVERLAY: toggle ignored, no workbench url";
}

BEGIN_METADATA(EnvOverlayHost)
END_METADATA

}  // namespace env
