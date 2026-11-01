// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_ai_icon.h"

#include "components/vector_icons/cc_macros.h"
#include "ui/gfx/vector_icon_types.h"

// The command names (MOVE_TO, R_LINE_TO, ...) live in namespace gfx; the generated icon files pull them in with this
// pattern, and `cc_macros.h` already defines VECTOR_ICON_ID_PREFIX for us.
#define DECLARE_VECTOR_COMMAND(x) using gfx::x;
DECLARE_VECTOR_COMMANDS

namespace env {

// A four-point sparkle plus a smaller one: the shape AI affordances use everywhere. Two filled sub-paths keep it legible
// at 16-20 px, where a stroked glyph would turn into a blob.
VECTOR_ICON_REP_TEMPLATE(kEnvAiSparklePath, CANVAS_DIMENSIONS, 20,
  FILL_RULE_NONZERO,
  // main sparkle: outer radius 6.4 around (8, 8), waist 1.9
  MOVE_TO, 8.0f, 1.6f,
  R_LINE_TO, 1.34f, 5.06f,
  R_LINE_TO, 5.06f, 1.34f,
  R_LINE_TO, -5.06f, 1.34f,
  R_LINE_TO, -1.34f, 5.06f,
  R_LINE_TO, -1.34f, -5.06f,
  R_LINE_TO, -5.06f, -1.34f,
  R_LINE_TO, 5.06f, -1.34f,
  CLOSE,
  // small sparkle: outer radius 3.0 around (15.4, 15.2), waist 0.9
  MOVE_TO, 15.4f, 12.2f,
  R_LINE_TO, 0.64f, 2.36f,
  R_LINE_TO, 2.36f, 0.64f,
  R_LINE_TO, -2.36f, 0.64f,
  R_LINE_TO, -0.64f, 2.36f,
  R_LINE_TO, -0.64f, -2.36f,
  R_LINE_TO, -2.36f, -0.64f,
  R_LINE_TO, 2.36f, -0.64f,
  CLOSE)

VECTOR_ICON_TEMPLATE_CC(kEnvAiSparkleRepList, kEnvAiSparkleIcon,
                        {kEnvAiSparklePath})

}  // namespace env
