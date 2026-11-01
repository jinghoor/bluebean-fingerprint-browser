// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_assistant_button.h"

#include "chrome/browser/env/env_ai_icon.h"
#include "ui/base/metadata/metadata_impl_macros.h"
#include "ui/color/color_id.h"
#include "ui/gfx/vector_icon_types.h"

namespace env {

EnvAssistantButton::EnvAssistantButton(PressedCallback callback)
    : ToolbarButton(std::move(callback)) {
  // An explicit AI button: the sidebar is gone from this product, so the toolbar carries one obvious entry point
  // with a visible "AI" label instead of an icon whose meaning had to be guessed.
  // The stock glyphs were a chat bubble or a wand, neither of which reads as "AI" - this is the product's own sparkle.
  SetImageModel(Button::STATE_NORMAL,
                ui::ImageModel::FromVectorIcon(env::kEnvAiSparkleIcon,
                                               kColorToolbarButtonIcon, 20));
  // Icon only: the label made the button wide and the operator wants the glyph alone. The tooltip keeps the shortcut
  // discoverable, and the accessible name keeps it announced.
  SetTooltipText(u"\u663e\u793a\u6216\u9690\u85cf DSH \u4fa7\u8fb9\u680f (Ctrl+Shift+W)");
  SetAccessibleName(u"DSH \u4fa7\u8fb9\u680f");
  SetHorizontalAlignment(gfx::ALIGN_CENTER);
  SetMinSize(gfx::Size(34, 28));
}

EnvAssistantButton::~EnvAssistantButton() = default;

BEGIN_METADATA(EnvAssistantButton)
END_METADATA

}  // namespace env
