// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/core/frame/navigator_fingerprint.h"

#include <array>
#include <memory>
#include <optional>
#include <string>
#include <string_view>

#include "third_party/blink/renderer/platform/graphics/fingerprint_seed.h"
#include "base/environment.h"
#include "base/no_destructor.h"
#include "third_party/blink/renderer/core/timezone/timezone_controller.h"
#include "third_party/icu/source/i18n/unicode/timezone.h"

namespace blink {
namespace {

// Environment variable carrying the per-profile seed.
constexpr char kSeedVar[] = "CHROME_FP_SEED";

// Owns the timezone override for the lifetime of the renderer. TimeZoneController
// releases the override when the handle dies, so it has to outlive every frame in
// this process -- a NoDestructor keeps it alive until process exit without a
// static destructor ordering problem.
std::unique_ptr<TimeZoneController::TimeZoneOverride>& TimezoneOverrideHandle() {
  static base::NoDestructor<
      std::unique_ptr<TimeZoneController::TimeZoneOverride>>
      handle;
  return *handle;
}

// FNV-1a, 64 bit. Chosen because it is tiny, allocation-free and stable across
// platforms -- we only need determinism, not cryptographic strength. Anyone who
// can read the seed can reproduce the values, and that is fine: the seed lives
// on the operator's own machine.
constexpr uint64_t kFnvOffset = 1469598103934665603ULL;
constexpr uint64_t kFnvPrime = 1099511628211ULL;

// Takes a string_view on purpose: Chromium builds with -Wunsafe-buffer-usage as
// an error, and both raw indexing and pointer walking over a plain const char*
// trip it. A range-based loop over a view is clean.
uint64_t Fnv1a(uint64_t hash, std::string_view s) {
  for (char c : s) {
    hash ^= static_cast<uint8_t>(c);
    hash *= kFnvPrime;
  }
  return hash;
}

// Hardware concurrency and memory are split by GPU tier rather than drawn from
// one flat list. The flat version produced profiles like "4 cores + RTX 3080 Ti"
// and "4 GB RAM + discrete GPU", which no real machine has -- a 3080 Ti costs
// more than such a build, and 4 GB would not run modern Windows comfortably on
// hardware someone bought a discrete card for. An audit of 200 profiles showed
// roughly half carried at least one of those contradictions.
//
// Values stay restricted to what real consumer machines report; odd numbers like
// 7 or 13 are a classic "driven by a tool" tell.
constexpr std::array<unsigned, 5> kCoresLow = {4, 6, 8, 12, 16};
constexpr std::array<unsigned, 5> kCoresMid = {6, 8, 12, 16, 20};
constexpr std::array<unsigned, 6> kCoresHigh = {8, 12, 16, 20, 24, 32};

constexpr std::array<float, 4> kMemoryLow = {4.0f, 8.0f, 16.0f, 32.0f};
constexpr std::array<float, 3> kMemoryMid = {8.0f, 16.0f, 32.0f};
constexpr std::array<float, 2> kMemoryHigh = {16.0f, 32.0f};

// Index into a std::array by a derived value. Range-for rather than operator[]
// because the latter trips -Wunsafe-buffer-usage in this codebase.
template <typename T, size_t N>
T PickBySeed(uint64_t derived, const std::array<T, N>& table) {
  const size_t target = static_cast<size_t>(derived % N);
  size_t index = 0;
  for (const T& value : table) {
    if (index++ == target) {
      return value;
    }
  }
  return *table.begin();
}

// Platform strings must agree with the UA string handed out by the launcher.
// Batch 1 targets Windows builds only, so Win32 is the single entry; the table
// exists so adding other platforms later is a one-line change.
constexpr std::array<const char*, 1> kPlatforms = {"Win32"};

// Accept-Language sets. Kept as comma-joined strings to line up with
// NavigatorLanguage::ParseAndSanitize().
//
// Each entry carries the IANA timezones that plausibly go with it. Timezone is
// NOT drawn independently: "zh-CN,zh" together with "America/New_York" is the
// kind of mismatch a geo-consistency check finds instantly, and it costs nothing
// to derive one from the other. The launcher picks the proxy by the same rule.
struct LocaleProfile {
  const char* languages;
  const char* timezones[2];  // nullptr terminates the candidate list
};
constexpr std::array<LocaleProfile, 7> kLocales = {{
    // Only canonical IANA names belong here. Asia/Chongqing is a tzdata link to
    // Asia/Shanghai and ICU rewrites it on the way out, so the browser reported a
    // different zone than the one derived -- verified against all seven
    // candidates: six round-trip exactly, Chongqing does not.
    {"zh-CN,zh", {"Asia/Shanghai", nullptr}},
    {"zh-CN,zh,en", {"Asia/Shanghai", nullptr}},
    {"en-US,en", {"America/New_York", "America/Los_Angeles"}},
    {"en-US,en,zh-CN", {"America/New_York", nullptr}},
    {"zh-TW,zh", {"Asia/Taipei", nullptr}},
    {"ja-JP,ja", {"Asia/Tokyo", nullptr}},
    {"ko-KR,ko", {"Asia/Seoul", nullptr}},
}};

// Hardware tier, used to keep GPU and screen from contradicting each other.
// A GeForce RTX 3080 Ti paired with a 1366x768 panel is not a machine anyone
// owns, and that pairing is trivially checkable -- the two values come from
// different calls with no dependency between them, so it is easy to forget.
enum HardwareTier : uint8_t {
  kTierLow = 0,   // integrated graphics, small laptop panel
  kTierMid = 1,   // mid-range discrete or good integrated, 1080p class
  kTierHigh = 2,  // high-end discrete, 1440p and up
};

// WebGL vendor/renderer pairs. Each pair is internally consistent: the vendor
// prefix must match the driver named inside the ANGLE renderer string, otherwise
// a page comparing the two immediately sees a contradiction.
struct GpuProfile {
  const char* vendor;
  const char* renderer;
  HardwareTier tier;
};
constexpr std::array<GpuProfile, 8> kGpuProfiles = {{
    {"Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Ti (0x00002208) Direct3D11 vs_5_0 "
     "ps_5_0, D3D11)",
     kTierHigh},
    {"Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     kTierHigh},
    {"Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, "
     "D3D11)",
     kTierMid},
    {"Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     kTierMid},
    {"Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     kTierHigh},
    {"Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
     kTierLow},
    {"Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     kTierLow},
    {"Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, "
     "D3D11)",
     kTierLow},
}};

// Audio perturbation magnitude.
//
// The first version used 1e-7, which turned out to be useless: the summed
// fingerprint sits near 124, and one float32 ulp at that magnitude is 1.48e-5.
// A 1e-7 shift therefore vanishes the moment a detector casts to float32, which
// every practical implementation does. Measured across 16 profiles: all 120
// pairs were within one ulp of each other, i.e. a detector saw 16 identical
// audio fingerprints and could conclude they came from one machine.
//
// 1e-3 gave ~68 float32 ulps per sample, but the summed fingerprint is a random
// walk over 500 samples, so the profile-to-profile spread came out at 2.9e-2 and
// the closest pair of 120 still fell inside one float32 ulp (5.1e-6 vs 1.5e-5).
//
// 1e-2 widens the walk to ~0.22 absolute, which is 0.18% of the ~124 total. Real
// machines with different audio hardware differ by 1-10% on this metric, so 0.18%
// stays inside the plausible range while guaranteeing every pair separates.
constexpr double kAudioEpsilon = 1.0e-2;

// Screen profiles. Real Windows desktop resolutions with the taskbar height
// already subtracted from the available rect (40 px at 100% scaling, which is
// the Windows 11 default).
//
// Every entry is 1080p or larger. Sub-1080p panels (1366x768, 1280x720,
// 1536x864) still exist but belong to low-end machines from several years ago,
// and a profile claiming a discrete GPU should not be driving one. The lowest
// entry here is 1080p, which is also what the majority of real desktops report.
//
// The `tier` column is the MINIMUM GPU tier that may pair with this panel, not
// an exact match: a high-end card driving 1080p is extremely common (competitive
// gamers do it deliberately), while an integrated GPU driving 4K is not.
//
// The launcher mirrors this table, so index order is part of the contract.
struct ScreenProfile {
  int width;
  int height;
  int avail_width;
  int avail_height;
  HardwareTier min_tier;
};
constexpr std::array<ScreenProfile, 7> kScreens = {{
    {1920, 1080, 1920, 1040, kTierLow},   // 1080p, the single most common panel
    {1920, 1200, 1920, 1160, kTierLow},   // 16:10 24" office monitor
    {2560, 1440, 2560, 1400, kTierMid},   // 1440p
    {2560, 1600, 2560, 1560, kTierMid},   // 1440p 16:10
    {2880, 1800, 2880, 1760, kTierHigh},  // 3K laptop panel (Surface class)
    {3440, 1440, 3440, 1400, kTierHigh},  // 21:9 ultrawide
    {3840, 2160, 3840, 2120, kTierHigh},  // 4K
}};

// Picks a panel that the given GPU tier can plausibly drive. Any screen whose
// minimum tier is at or below the GPU's tier qualifies, so better hardware
// unlocks more panels while lower tiers stay restricted to the common ones.
const ScreenProfile& PickScreenForTier(uint64_t derived, HardwareTier tier) {
  size_t eligible = 0;
  for (const ScreenProfile& screen : kScreens) {
    if (screen.min_tier <= tier) {
      ++eligible;
    }
  }
  if (eligible == 0) {
    // Cannot happen with the table above (kScreens[0] is kTierLow) but keeps the
    // function total.
    return kScreens[0];
  }
  size_t index = derived % eligible;
  for (const ScreenProfile& screen : kScreens) {
    if (screen.min_tier <= tier) {
      if (index-- == 0) {
        return screen;
      }
    }
  }
  return kScreens[0];
}

}  // namespace

NavigatorFingerprint::NavigatorFingerprint() {
  // Prefer the per-partition seed published through RendererPreferences. A
  // renderer process hosts exactly one StoragePartition, so a process-wide seed
  // is a per-environment seed -- that is what keeps two environments from
  // ending up with the same fingerprint. Falls back to the command-line seed,
  // which is how a single-environment launch has always been configured.
  std::string seed_storage;
  std::string_view partition_seed = GetFingerprintSeed();
  if (!partition_seed.empty()) {
    seed_storage.assign(partition_seed);
  } else {
    std::unique_ptr<base::Environment> env = base::Environment::Create();
    if (!env) {
      return;
    }
    // Newer base::Environment::GetVar returns std::optional<std::string> instead
    // of taking an out-parameter.
    std::optional<std::string> value = env->GetVar(kSeedVar);
    if (!value || value->empty()) {
      // Stock behaviour. Leave the scalars at harmless defaults; every call site
      // is gated on active().
      return;
    }
    seed_storage = *value;
  }

  const std::string& seed = seed_storage;
  base_ = Fnv1a(kFnvOffset, seed);
  active_ = true;

  navigator_platform_ = String(kPlatforms[Derive("pf") % kPlatforms.size()]);

  // Environment settings own language when present. This keeps navigator,
  // Intl (set from the same environment locale), and the request header aligned.
  // Standalone launches retain their deterministic seed-derived profile.
  const uint64_t language_draw = Derive("lang");
  const LocaleProfile* locale = &kLocales[language_draw % kLocales.size()];
  const std::string_view configured_languages = GetFingerprintLanguages();
  if (!configured_languages.empty()) {
    languages_ = String::FromUtf8(configured_languages);
    const size_t comma = configured_languages.find(',');
    const std::string_view primary = configured_languages.substr(0, comma);
    for (const LocaleProfile& candidate : kLocales) {
      const std::string_view candidate_languages(candidate.languages);
      const size_t candidate_comma = candidate_languages.find(',');
      if (candidate_languages.substr(0, candidate_comma) == primary) {
        locale = &candidate;
        break;
      }
    }
  } else {
    languages_ = String(locale->languages);
  }

  // Count first, then walk again to the chosen slot. Indexing the C array
  // directly trips -Wunsafe-buffer-usage, and range-for keeps this readable.
  size_t timezone_count = 0;
  for (const char* tz : locale->timezones) {
    if (tz == nullptr) {
      break;
    }
    ++timezone_count;
  }
  if (timezone_count > 0) {
    const size_t pick = Derive("tz") % timezone_count;
    size_t index = 0;
    for (const char* tz : locale->timezones) {
      if (tz == nullptr) {
        break;
      }
      if (index++ == pick) {
        timezone_id_ = String(tz);
        break;
      }
    }
  }

  const GpuProfile& gpu = kGpuProfiles[Derive("gpu") % kGpuProfiles.size()];

  // Integrated parts cannot report discrete-card capability limits.
  // Intel's D3D11 stack caps MAX_VIEWPORT_DIMS at 16384 and the aliased
  // point size at 255; advertising 32767 / 1024 alongside a renderer
  // string that says "Intel UHD Graphics 630" is a single-call
  // contradiction. Only these two are overridden: they have documented
  // per-vendor ceilings. Values I cannot state precisely (MAX_VARYING_
  // VECTORS, for instance) are left as the real GPU reports them.
  if (gpu.tier == kTierLow) {
    webgl_viewport_max_ = 16384;
    webgl_point_size_max_ = 255;
  }
  webgl_vendor_ = String(gpu.vendor);
  webgl_renderer_ = String(gpu.renderer);

  // Cores and RAM follow the GPU tier for the same reason the screen does: the
  // three get compared against each other, and a mismatch is free to detect.
  switch (gpu.tier) {
    case kTierHigh:
      hardware_concurrency_ = PickBySeed(Derive("hw"), kCoresHigh);
      device_memory_ = PickBySeed(Derive("dm"), kMemoryHigh);
      break;
    case kTierMid:
      hardware_concurrency_ = PickBySeed(Derive("hw"), kCoresMid);
      device_memory_ = PickBySeed(Derive("dm"), kMemoryMid);
      break;
    case kTierLow:
    default:
      hardware_concurrency_ = PickBySeed(Derive("hw"), kCoresLow);
      device_memory_ = PickBySeed(Derive("dm"), kMemoryLow);
      break;
  }

  // Screen tier follows the GPU tier: the two get compared against each other,
  // and a high-end card driving a 1366x768 panel is not a real machine.
  const ScreenProfile& screen = PickScreenForTier(Derive("scr"), gpu.tier);
  screen_width_ = screen.width;
  screen_height_ = screen.height;
  screen_avail_width_ = screen.avail_width;
  screen_avail_height_ = screen.avail_height;

  // Downlink between 5.0 and 15.0 Mbps in 0.5 steps, RTT between 50 and 150 ms.
  // Both are what a domestic broadband or WiFi link plausibly reports; the raw
  // value on this host was 1.55 Mbps, which stands out against the 4g
  // effectiveType it reports alongside.
  network_downlink_mbps_ = 5.0 + static_cast<double>(Derive("dl") % 21) * 0.5;
  network_rtt_ms_ = 50 + static_cast<int>(Derive("rtt") % 101);

  // Push the timezone into ICU. Both Intl.DateTimeFormat().resolvedOptions()
  // .timeZone and Date.prototype.getTimezoneOffset() resolve through
  // icu::TimeZone::createDefault(), so the process default covers both without
  // touching V8. This must run before either is first called, which is why
  // LocalDOMWindow forces NavigatorFingerprint::Get() during construction.
  //
  // It goes through TimeZoneController rather than calling ICU directly. Two
  // things happen there that this code needs:
  //   - V8 is told the default zone changed. Without that notification an
  //     isolate created earlier keeps reporting the zone it was born with, which
  //     is exactly the case in a renderer reused for an environment tab.
  //   - The value is installed as an *override*, so the browser's device
  //     timezone notification cannot put the host zone back. Measured before
  //     this change: an environment tab reported the seeded zone for a moment,
  //     then fell back to the host zone, while a --dump-dom launch (which exits
  //     before that notification arrives) kept it. That is why every environment
  //     used to look like it was in the same country.
  if (!timezone_id_.empty()) {
    TimeZoneController::TimeZoneOverrideResult result =
        TimeZoneController::SetTimeZoneOverride(String(timezone_id_));
    if (result.handle) {
      TimezoneOverrideHandle() = std::move(result.handle);
    }
  }
}

uint64_t NavigatorFingerprint::Derive(std::string_view salt) const {
  // Chain the salt so derived values are independent of each other: a change in
  // one field must not imply anything about the next.
  uint64_t h = Fnv1a(base_, ":");
  h = Fnv1a(h, salt);

  // FNV-1a's low bits are visibly non-uniform, which matters because every
  // consumer reduces this with % table_size and the tables are small (8, 10,
  // 7 entries). Without a finalizer the GPU draw collapsed onto three of eight
  // entries across six test accounts -- every profile landed in the same tier.
  // splitmix64's finalizer fixes the avalanche for a couple of instructions.
  h ^= h >> 30;
  h *= 0xBF58476D1CE4E5B9ULL;
  h ^= h >> 27;
  h *= 0x94D049BB133111EBULL;
  h ^= h >> 31;
  return h;
}

double NavigatorFingerprint::audio_delta(size_t index) const {
  if (!active_) {
    return 0.0;
  }
  // splitmix64-style avalanche so that adjacent indices do not produce
  // correlated offsets. Without this, a detector averaging neighbours would see
  // the perturbation wash out.
  uint64_t h = base_ ^ (static_cast<uint64_t>(index) * 0x9E3779B97F4A7C15ULL);
  h ^= h >> 30;
  h *= 0xBF58476D1CE4E5B9ULL;
  h ^= h >> 27;
  h *= 0x94D049BB133111EBULL;
  h ^= h >> 31;

  // Map the low 16 bits into [-0.5, 0.5), then scale to +/- kAudioEpsilon.
  const double unit = static_cast<double>(h & 0xFFFF) / 65536.0;
  return (unit - 0.5) * 2.0 * kAudioEpsilon;
}

int NavigatorFingerprint::canvas_delta(int x,
                                       int y,
                                       uint32_t channel) const {
  if (!active_) {
    return 0;
  }
  // Must stay byte-identical in behaviour to CanvasNoise::Apply() in
  // platform/graphics/image_data_buffer.cc: same seed, same mixing, same
  // 1-in-1013 selection, same channel choice. Otherwise a page could compare a
  // toDataURL() render against a getImageData() read of the same pixels and
  // spot that the two disagree.
  uint64_t h = base_ ^ (static_cast<uint64_t>(y) << 32) ^
               static_cast<uint64_t>(x);
  h *= 0x9E3779B97F4A7C15ULL;
  h ^= h >> 29;
  if ((h % 1013) != 0) {
    return 0;
  }
  // Alpha is never touched.
  if ((h >> 11) % 3 != channel) {
    return 0;
  }
  return ((h >> 23) & 1) ? 1 : -1;
}

int NavigatorFingerprint::webgl_delta(int x,
                                      int y,
                                      uint32_t channel) const {
  if (!active_) {
    return 0;
  }
  // Sparse selection, one non-alpha channel, +/-1 -- but denser than
  // canvas_delta's 1-in-1013.
  //
  // Detectors do not always read the whole framebuffer. CreepJS reads only
  // drawingBufferWidth/15 by drawingBufferHeight/6, which is 17x42 on a 256x256
  // canvas: 714 texels, 2142 eligible channel slots. At 1-in-1013 that is 2.1
  // expected hits, so roughly half of all seeds land zero hits and the readback
  // comes back pixel-identical to every other profile. 1-in-97 puts ~22 hits in
  // the same window while still touching only ~1% of channels at full size,
  // which is invisible at +/-1 LSB.
  uint64_t h = base_ ^ (static_cast<uint64_t>(y) << 32) ^
               static_cast<uint64_t>(x) ^ 0x574542474CDEAD00ULL;
  h *= 0x9E3779B97F4A7C15ULL;
  h ^= h >> 29;
  if ((h % 97) != 0) {
    return 0;
  }
  if ((h >> 11) % 3 != channel) {
    return 0;
  }
  return ((h >> 23) & 1) ? 1 : -1;
}

const NavigatorFingerprint& NavigatorFingerprint::Get() {
  // One instance per renderer process. base::NoDestructor avoids a static
  // destructor running after Blink's PartitionAlloc is gone.
  static base::NoDestructor<NavigatorFingerprint> instance;
  return *instance;
}

}  // namespace blink
