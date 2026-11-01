// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef THIRD_PARTY_BLINK_RENDERER_CORE_FRAME_NAVIGATOR_FINGERPRINT_H_
#define THIRD_PARTY_BLINK_RENDERER_CORE_FRAME_NAVIGATOR_FINGERPRINT_H_

#include <cstdint>
#include <string_view>

#include "third_party/blink/renderer/core/core_export.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"

namespace blink {

// Deterministic per-profile fingerprint overrides.
//
// The whole thing is driven by a single environment variable read once per
// renderer process:
//
//     CHROME_FP_SEED=<opaque-string>
//
// The caller is expected to derive that string deterministically from a stable
// account identifier (see fp_launcher.py), so that:
//   * the same account always boots with the same fingerprint, and
//   * different accounts get different fingerprints.
//
// When the variable is absent or empty, active() is false and every call site
// falls through to stock Chromium behaviour. That keeps a vanilla build of this
// tree byte-identical to upstream, which is what the fingerprint comparison
// tests rely on.
//
// Everything is computed in C++ so JS-visible values come from native code
// paths: no wrapper functions, no proxied getters, nothing a page can notice via
// Function.prototype.toString / property descriptors / Error.stack shape.
class CORE_EXPORT NavigatorFingerprint {
 public:
  static const NavigatorFingerprint& Get();

  NavigatorFingerprint(const NavigatorFingerprint&) = delete;
  NavigatorFingerprint& operator=(const NavigatorFingerprint&) = delete;

  // Public because base::NoDestructor needs to construct it.
  NavigatorFingerprint();

  // False => do not touch anything, this is a stock Chromium build.
  bool active() const { return active_; }

  // navigator.webdriver. Hardcoded false: a real user's browser never reports
  // true, and the stock path ORs into the value so it can only ever be flipped
  // on, never off -- see InspectorEmulationAgent::ApplyAutomationOverride.
  bool webdriver() const { return false; }

  unsigned hardware_concurrency() const { return hardware_concurrency_; }
  float device_memory() const { return device_memory_; }

  // Empty String => caller keeps the native value.
  const String& navigator_platform() const { return navigator_platform_; }
  // Environment language settings override the seeded locale when provided.
  const String& languages() const { return languages_; }

  // WebGL: both empty when inactive, in which case the caller must fall back to
  // the real driver strings from ContextGL().
  const String& webgl_vendor() const { return webgl_vendor_; }
  const String& webgl_renderer() const { return webgl_renderer_; }

  // 0 means "report what the GPU actually says". Non-zero applies only
  // to profiles impersonating integrated graphics, whose hardware cannot
  // reach discrete-card limits.
  int webgl_viewport_max() const { return webgl_viewport_max_; }
  int webgl_point_size_max() const { return webgl_point_size_max_; }

  // IANA timezone id ("Asia/Shanghai"). Empty when inactive. Derived from the
  // language rather than independently, so the two cannot contradict each other.
  // Applying it to ICU happens in the constructor; see the note there.
  const String& timezone_id() const { return timezone_id_; }

  // NetworkInformation. 0 means "leave the real value alone". effectiveType is
  // deliberately not overridden: every WiFi and wired connection reports "4g",
  // so varying it would be the odd thing out.
  double network_downlink_mbps() const { return network_downlink_mbps_; }
  int network_rtt_ms() const { return network_rtt_ms_; }

  // Audio: deterministic tiny offset for sample |index| of a rendered buffer.
  // Magnitude ~1e-7 -- below any audible threshold, but enough to change the
  // summed fingerprint float that detectors compare across machines.
  double audio_delta(size_t index) const;

  // Canvas: whether the pixel at (x, y) should be nudged, and by how much.
  // Returns 0 for the vast majority of pixels -- only ~1 in 1000 is touched.
  // Used by the getImageData() path; the toDataURL() path has its own copy in
  // platform/graphics/image_data_buffer.cc because platform cannot depend on
  // core.
  int canvas_delta(int x, int y, uint32_t channel) const;

  // Per-pixel perturbation for WebGL readPixels, indexed by framebuffer
  // coordinates. Uses a different mixing constant than canvas_delta so the
  // two perturbation patterns cannot be correlated with each other, while
  // both still derive from the same seed.
  int webgl_delta(int x, int y, uint32_t channel) const;

  // Screen geometry. Returns 0 when inactive, which every caller treats as
  // "keep the real value". colorDepth is deliberately NOT derived: desktop
  // Windows always reports 24 and anything else stands out immediately.
  //
  // The launcher derives the same profile from the same seed and sizes the
  // window to fit inside availWidth/availHeight -- a window wider than the
  // reported screen is the single most obvious inconsistency in this area.
  int screen_width() const { return screen_width_; }
  int screen_height() const { return screen_height_; }
  int screen_avail_width() const { return screen_avail_width_; }
  int screen_avail_height() const { return screen_avail_height_; }

  // Exposed for diagnostics / launcher cross-checks.
  uint64_t seed_hash() const { return base_; }

 private:
  // Stable mix of the base seed with a domain separator.
  uint64_t Derive(std::string_view salt) const;

  bool active_ = false;
  uint64_t base_ = 0;
  unsigned hardware_concurrency_ = 8;
  float device_memory_ = 8.0f;
  String navigator_platform_;
  String languages_;
  String webgl_vendor_;
  String webgl_renderer_;
  int webgl_viewport_max_ = 0;
  int webgl_point_size_max_ = 0;
  String timezone_id_;
  double network_downlink_mbps_ = 0.0;
  int network_rtt_ms_ = 0;
  int screen_width_ = 0;
  int screen_height_ = 0;
  int screen_avail_width_ = 0;
  int screen_avail_height_ = 0;
};

}  // namespace blink

#endif  // THIRD_PARTY_BLINK_RENDERER_CORE_FRAME_NAVIGATOR_FINGERPRINT_H_
