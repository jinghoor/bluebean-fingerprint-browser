// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef THIRD_PARTY_BLINK_RENDERER_PLATFORM_GRAPHICS_FINGERPRINT_SEED_H_
#define THIRD_PARTY_BLINK_RENDERER_PLATFORM_GRAPHICS_FINGERPRINT_SEED_H_

#include <string>
#include <string_view>

namespace blink {

// Process-wide holder for the fingerprint seed.
//
// It exists because the two halves of the fingerprint live on different layers:
// NavigatorFingerprint is in core, the canvas perturbation is in platform, and
// platform must not depend on core. A plain function pair in platform lets both
// read the same value without inventing a dependency.
//
// "Process-wide" is the right scope here rather than "per page": a renderer
// process is bound to a single StoragePartition (that is what makes environments
// isolated in the first place), so one seed per process is exactly one seed per
// environment. See env_manager.cc for the partition assignment.
//
// Empty means "not configured": callers fall back to their previous behaviour,
// which keeps the command-line driven path working unchanged.
void SetFingerprintSeed(std::string_view seed);
std::string_view GetFingerprintSeed();
void SetFingerprintLanguages(std::string_view languages);
std::string_view GetFingerprintLanguages();

}  // namespace blink

#endif  // THIRD_PARTY_BLINK_RENDERER_PLATFORM_GRAPHICS_FINGERPRINT_SEED_H_
