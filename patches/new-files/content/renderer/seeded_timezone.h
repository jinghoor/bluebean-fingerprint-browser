// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CONTENT_RENDERER_SEEDED_TIMEZONE_H_
#define CONTENT_RENDERER_SEEDED_TIMEZONE_H_

#include <string>

namespace content {

// Reads CHROME_FP_SEED directly from the environment and derives the IANA
// timezone this profile should report.
//
// Lives here rather than next to blink::NavigatorFingerprint for one reason:
// timing. V8 constructs a DateCache inside every Isolate, and DateCache's
// constructor immediately latches icu::TimeZone::createDefault() into its
// timezone cache. In a renderer, the Isolate can be created during
// blink::Platform::InitializeBlink / RendererMainPlatformDelegate, which is far
// earlier than the first LocalDOMWindow -- and with spare renderers (triggered by
// --renderer-process-limit<=2) it is *always* earlier. Anything that sets the ICU
// default after that point has no effect on Date or Intl for that process.
//
// The window between ICU being initialised (content main) and V8's Isolate being
// created (RendererMain) is the only place that works, and Blink's WTF strings
// are not usable there -- hence a base-only implementation that duplicates the
// derivation. That duplication is deliberate and is cross-checked by the audit
// tooling: both copies read the same locale table shape and the same FNV-1a +
// splitmix64 derivation.
//
// Returns an empty string when no seed is present (stock behaviour).
std::string GetSeededTimezoneId();

// Pushes the seeded timezone into ICU as the process default. No-op when no seed
// is set or the derived id is empty. Idempotent and safe to call early.
//
// Call this from RendererMain before the Blink platform is initialised.
void ApplySeededTimezoneToIcu();

}  // namespace content

#endif  // CONTENT_RENDERER_SEEDED_TIMEZONE_H_
