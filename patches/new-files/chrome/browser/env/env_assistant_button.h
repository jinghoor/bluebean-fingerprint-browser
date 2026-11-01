// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_ASSISTANT_BUTTON_H_
#define CHROME_BROWSER_ENV_ENV_ASSISTANT_BUTTON_H_

#include "chrome/browser/ui/views/toolbar/toolbar_button.h"
#include "ui/base/metadata/metadata_header_macros.h"

namespace env {

// Toolbar entry point for the assistant layer: same command as Ctrl+Shift+W, so the button and the
// accelerator cannot drift apart.
class EnvAssistantButton : public ToolbarButton {
  METADATA_HEADER(EnvAssistantButton, ToolbarButton)

 public:
  explicit EnvAssistantButton(PressedCallback callback);
  EnvAssistantButton(const EnvAssistantButton&) = delete;
  EnvAssistantButton& operator=(const EnvAssistantButton&) = delete;
  ~EnvAssistantButton() override;
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_ASSISTANT_BUTTON_H_
