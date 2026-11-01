// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_sidebar_panel_view.h"

#include "chrome/browser/profiles/profile.h"
#include "content/public/browser/navigation_controller.h"
#include "content/public/browser/web_contents.h"
#include "ui/base/metadata/metadata_impl_macros.h"
#include "ui/base/page_transition_types.h"
#include "ui/views/controls/webview/webview.h"
#include "ui/views/layout/fill_layout.h"

namespace env {

EnvSidebarPanelView::EnvSidebarPanelView(Profile* profile, const GURL& url) {
  SetLayoutManager(std::make_unique<views::FillLayout>());

  // Own the WebContents here and hand the view a non-owning pointer: the side panel recreates its content view when it
  // is shown or hidden, and `SetOwnedWebContents` made the browser exit right after the panel appeared (measured).
  contents_ = content::WebContents::Create(content::WebContents::CreateParams(profile));
  contents_->GetController().LoadURL(url, content::Referrer(),
                                     ui::PAGE_TRANSITION_AUTO_TOPLEVEL, std::string());

  auto web_view = std::make_unique<views::WebView>(profile);
  web_view_ = AddChildView(std::move(web_view));
  web_view_->SetWebContents(contents_.get());

  // No C++ CSS injection: the plugin inside the page detects the narrow host and hides the main column itself, which
  // keeps this view free of observers pointing at a WebContents the panel may replace.
  LOG(ERROR) << "ENVSIDEBAR: panel created url=" << url.spec().substr(0, 60);
}

EnvSidebarPanelView::~EnvSidebarPanelView() {
  if (web_view_) {
    web_view_->SetWebContents(nullptr);
  }
}

BEGIN_METADATA(EnvSidebarPanelView)
END_METADATA

}  // namespace env
