// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "content/renderer/seeded_timezone.h"

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

#include "base/environment.h"
#include "base/logging.h"
#include "third_party/icu/source/i18n/unicode/timezone.h"
#include "third_party/icu/source/common/unicode/unistr.h"

namespace content {
namespace {

constexpr char kSeedVar[] = "CHROME_FP_SEED";

constexpr uint64_t kFnvOffset = 1469598103934665603ULL;
constexpr uint64_t kFnvPrime = 1099511628211ULL;

uint64_t Fnv1a(uint64_t hash, std::string_view s) {
  for (char c : s) {
    hash ^= static_cast<uint8_t>(c);
    hash *= kFnvPrime;
  }
  return hash;
}

// Must mirror kLocales in
// third_party/blink/renderer/core/frame/navigator_fingerprint.cc: same order,
// same strings, same timezone candidates. The browser picks the locale by
// index, so a mismatch here silently produces a profile whose Intl timezone
// disagrees with its navigator.languages.
//
// Only canonical IANA ids belong here -- Asia/Chongqing is a tzdata link to
// Asia/Shanghai and ICU rewrites it on the way out.
struct LocaleEntry {
  const char* languages;
  const char* timezones[2];
};
constexpr std::array<LocaleEntry, 7> kLocales = {{
    {"zh-CN,zh", {"Asia/Shanghai", nullptr}},
    {"zh-CN,zh,en", {"Asia/Shanghai", nullptr}},
    {"en-US,en", {"America/New_York", "America/Los_Angeles"}},
    {"en-US,en,zh-CN", {"America/New_York", nullptr}},
    {"zh-TW,zh", {"Asia/Taipei", nullptr}},
    {"ja-JP,ja", {"Asia/Tokyo", nullptr}},
    {"ko-KR,ko", {"Asia/Seoul", nullptr}},
}};

uint64_t Derive(uint64_t base, std::string_view salt) {
  uint64_t h = Fnv1a(base, ":");
  h = Fnv1a(h, salt);
  // splitmix64 finalizer; FNV-1a's low bits are non-uniform and every consumer
  // reduces with % table_size.
  h ^= h >> 30;
  h *= 0xBF58476D1CE4E5B9ULL;
  h ^= h >> 27;
  h *= 0x94D049BB133111EBULL;
  h ^= h >> 31;
  return h;
}

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

}  // namespace

std::string GetSeededTimezoneId() {
  std::unique_ptr<base::Environment> env = base::Environment::Create();
  if (!env) {
    return std::string();
  }
  std::optional<std::string> seed = env->GetVar(kSeedVar);
  if (!seed || seed->empty()) {
    return std::string();
  }

  const uint64_t base = Fnv1a(kFnvOffset, *seed);
  const LocaleEntry& locale = PickBySeed(Derive(base, "lang"), kLocales);

  size_t timezone_count = 0;
  for (const char* tz : locale.timezones) {
    if (tz == nullptr) {
      break;
    }
    ++timezone_count;
  }
  if (timezone_count == 0) {
    return std::string();
  }

  const size_t pick = static_cast<size_t>(Derive(base, "tz") % timezone_count);
  size_t index = 0;
  for (const char* tz : locale.timezones) {
    if (tz == nullptr) {
      break;
    }
    if (index++ == pick) {
      return std::string(tz);
    }
  }
  return std::string();
}

void ApplySeededTimezoneToIcu() {
  const std::string id = GetSeededTimezoneId();
  if (id.empty()) {
    return;
  }

  icu::UnicodeString icu_id = icu::UnicodeString::fromUTF8(id);
  icu::TimeZone* zone = icu::TimeZone::createTimeZone(icu_id);
  if (!zone) {
    return;
  }
  icu::TimeZone::setDefault(*zone);

  // ICU canonicalises on the way out; note when it does so the discrepancy is
  // visible rather than silent.
  icu::UnicodeString actual;
  zone->getID(actual);
  std::string actual_id;
  actual.toUTF8String(actual_id);
  if (actual_id != id) {
    VLOG(1) << "seeded timezone " << id << " canonicalised to " << actual_id;
  }
  delete zone;
}

}  // namespace content
