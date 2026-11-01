// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_SIDEBAR_PANEL_VIEW_H_
#define CHROME_BROWSER_ENV_ENV_SIDEBAR_PANEL_VIEW_H_

#include <memory>

#include "base/memory/raw_ptr.h"
#include "ui/views/view.h"
#include "url/gurl.h"

class Profile;

namespace content {
class WebContents;
}

namespace views {
class WebView;
}

namespace env {

// DSH's own sidebar, docked in Chrome's side panel so the page is laid out to its right.
//
// The view is deliberately dumb: one WebView on the workbench URL, and the plugin inside the page hides the workbench's
// main column when the host is narrow. The conversation lives in the floating layer instead (env/env_overlay.h).
class EnvSidebarPanelView : public views::View {
  METADATA_HEADER(EnvSidebarPanelView, views::View)

 public:
  EnvSidebarPanelView(Profile* profile, const GURL& url);
  EnvSidebarPanelView(const EnvSidebarPanelView&) = delete;
  EnvSidebarPanelView& operator=(const EnvSidebarPanelView&) = delete;
  ~EnvSidebarPanelView() override;

 private:
  // Declared before the view so it outlives the child view during destruction.
  std::unique_ptr<content::WebContents> contents_;
  raw_ptr<views::WebView> web_view_ = nullptr;
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_SIDEBAR_PANEL_VIEW_H_
