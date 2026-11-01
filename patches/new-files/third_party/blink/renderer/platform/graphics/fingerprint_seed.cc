// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/platform/graphics/fingerprint_seed.h"

#include <string>

#include "base/no_destructor.h"

namespace blink {
namespace {

// NoDestructor so the string is never torn down at exit: the renderer can be
// tearing down while a final read happens, and leaking one small string is
// cheaper than ordering guarantees.
std::string& MutableSeed() {
  static base::NoDestructor<std::string> seed;
  return *seed;
}

std::string& MutableLanguages() {
  static base::NoDestructor<std::string> languages;
  return *languages;
}

}  // namespace

void SetFingerprintSeed(std::string_view seed) {
  MutableSeed().assign(seed);
}

std::string_view GetFingerprintSeed() {
  return MutableSeed();
}

void SetFingerprintLanguages(std::string_view languages) {
  MutableLanguages().assign(languages);
}

std::string_view GetFingerprintLanguages() {
  return MutableLanguages();
}

}  // namespace blink
