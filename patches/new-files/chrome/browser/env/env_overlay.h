// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_OVERLAY_H_
#define CHROME_BROWSER_ENV_ENV_OVERLAY_H_

#include <string>

#include "url/gurl.h"
#include "ui/gfx/native_ui_types.h"
#include "ui/gfx/geometry/point.h"
#include "ui/gfx/geometry/rect.h"

class BrowserWindowInterface;
class BrowserView;
namespace content {
class WebContents;
}
namespace views {
class View;
}

namespace env {

// Identity of the embedded assistant, set before its first navigation. Ordinary
// tabs visiting the same URL must still obey the environment startup gate.
bool IsAssistantWebContents(content::WebContents* contents);
// Native-owned assistant popups retain their original WebContents and default
// partition (including the login WindowProxy and loopback callback).
bool IsAssistantAuxiliaryWebContents(content::WebContents* contents);

// The assistant's sidebar reserves real content width. Its conversation floats
// above the page without changing that reservation. The same DSH WebContents
// stays alive through sidebar and conversation visibility changes.
bool ShowOverlay(BrowserWindowInterface* window, const GURL& url, std::string* error);
bool HideOverlay(BrowserWindowInterface* window);
bool OverlayVisible(BrowserWindowInterface* window);
// Reserve only the DSH sidebar in the browser's real content layout. The
// conversation extends above the resulting page without resizing it.
gfx::Rect ReserveAssistantSidebar(views::View* browser_view,
                                  const gfx::Rect& content_bounds);
// Reject input to the clipped-away native subtree without replacing its targeter.
bool OverlayAllowsNativeEvent(BrowserView* browser_view,
                              gfx::NativeView child,
                              const gfx::Point& root_point);

// Hide without hiding the renderer: the WebContents stays visible (so Chromium does not throttle its
// timers) while the view occupies 2x2 pixels. Used to decide the product's click-away behaviour.
bool ShrinkOverlay(BrowserWindowInterface* window);

// Overlay width override (0 = the product two-thirds rule). Needed because the DSH web client folds its
// sidebar into an icon rail below its own breakpoint, which hides the sidebar seats.
bool SetOverlayWidth(BrowserWindowInterface* window, int width);

// Fold the layer back to its session rail (the sidebar alone) and bring it out again. Collapsing keeps the
// workbench and any running task alive.
bool CollapseOverlayToRail(BrowserWindowInterface* window);
bool ExpandOverlayFromRail(BrowserWindowInterface* window);

// Entry point used by the browser command (toolbar button and Ctrl+Shift+W): shows the layer on the
// workbench, or hides it when it is already visible. The workbench URL is read from the state file
// the bridge maintains, so the command needs no Python round trip.
void ToggleAssistantLayer(BrowserWindowInterface* window);

// Show without toggling: a "show the panel" request must be idempotent, otherwise any code path that asks for the
// sidebar hides a layer the user already has open (measured while switching environments).
void ShowAssistantLayer(BrowserWindowInterface* window);

// The workbench URL from the product's state file (empty when unavailable): the side panel hosts the same workbench as
// its sidebar, so both surfaces point at one backend.
GURL WorkbenchUrlForProduct();
bool RestoreOverlay(BrowserWindowInterface* window);
// Reconnect an existing host without changing whether the user hid it.
bool RefreshOverlay(BrowserWindowInterface* window, const GURL& url);

// Rectangle the overlay host currently occupies, in the same coordinates as the command reports.
std::string OverlayGeometry(BrowserWindowInterface* window);

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_OVERLAY_H_
