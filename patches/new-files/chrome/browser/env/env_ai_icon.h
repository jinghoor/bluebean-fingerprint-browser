// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_ENV_AI_ICON_H_
#define CHROME_BROWSER_ENV_ENV_AI_ICON_H_

#include "ui/gfx/vector_icon_types.h"

namespace env {

// The product's AI mark: a four-point sparkle with a smaller one beside it. Drawn here instead of reusing a stock icon
// because every available glyph was a chat bubble or a wand, which the operator did not read as "AI".
extern const gfx::VectorIcon kEnvAiSparkleIcon;

}  // namespace env

#endif  // CHROME_BROWSER_ENV_ENV_AI_ICON_H_
