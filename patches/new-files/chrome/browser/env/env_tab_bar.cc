// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/env_tab_bar.h"

#include <algorithm>
#include <utility>

#include "base/functional/bind.h"
#include "base/logging.h"
#include "base/task/single_thread_task_runner.h"
#include "base/strings/string_number_conversions.h"
#include "base/strings/utf_string_conversions.h"
#include "ui/base/l10n/l10n_util.h"
#include "ui/base/ui_base_types.h"
#include "ui/color/color_id.h"
#include "ui/gfx/font_list.h"
#include "ui/views/background.h"
#include "ui/views/controls/button/label_button.h"
#include "ui/views/controls/label.h"
#include "ui/base/metadata/metadata_impl_macros.h"
#include "ui/views/layout/box_layout.h"

namespace env {
namespace {

constexpr int kChipHeight = 26;
constexpr size_t kMaxTitleChars = 22;

std::u16string Shorten(const std::u16string& title) {
  if (title.size() <= kMaxTitleChars) {
    return title;
  }
  return title.substr(0, kMaxTitleChars - 1) + u"\u2026";
}

}  // namespace

EnvTabBar::EnvTabBar(EnvManager* manager) : manager_(manager) {
  if (manager_) {
    manager_->AddObserver(this);
  }
  // Refreshing here would read the tab model while the window that hosts this bar is still being
  // constructed (and the read used to be able to create a window), so defer it one task.
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE, base::BindOnce(&EnvTabBar::Refresh, weak_factory_.GetWeakPtr()));
}

EnvTabBar::~EnvTabBar() {
  if (manager_) {
    manager_->RemoveObserver(this);
  }
}

void EnvTabBar::OnEnvsChanged() {
  Refresh();
}

void EnvTabBar::OnActiveEnvChanged(const std::string& env_id) {
  Refresh();
}

void EnvTabBar::OnTabsChanged() {
  Refresh();
}


void EnvTabBar::Layout(PassKey) {
  // Explicit left-to-right placement: a box layout kept reporting 0x0 children after a rebuild
  // because nothing invalidated the parent, so the chips never appeared.
  const int bar_height = this->height();
  const int bar_width = this->width();
  const int child_count = static_cast<int>(children().size());
  if (child_count == 0) {
    return;
  }

  constexpr int kPlusWidth = 32;
  constexpr int kCloseWidth = 22;
  constexpr int kEnvWidth = 96;
  constexpr int kMinChipWidth = 52;
  constexpr int kGap = 2;

  // Reservations first: the trailing "+" must stay reachable however many tabs there are.
  int available = bar_width - kEnvWidth - kPlusWidth - kGap * (child_count + 1);
  int chip_count = 0;
  for (int i = 1; i + 1 < child_count - 1; i += 2) {
    ++chip_count;
  }
  int chip_width = chip_count > 0 ? available / chip_count : 0;
  if (chip_width < kMinChipWidth) {
    chip_width = kMinChipWidth;
  }
  chip_width = std::min(chip_width, 220);

  int x = kGap;
  for (int i = 0; i < child_count; ++i) {
    views::View* child = children().at(i);
    int width = 0;
    if (i == 0) {
      width = kEnvWidth;
    } else if (i == child_count - 1) {
      width = kPlusWidth;
    } else {
      const bool is_close = ((i % 2) == 0);
      width = is_close ? kCloseWidth : chip_width;
    }
    if (x + width > bar_width) {
      // Only in the extreme case where even the minimum does not fit: hide the rest but keep "+".
      child->SetVisible(false);
      continue;
    }
    child->SetVisible(true);
    child->SetBounds(x, 0, width, bar_height);
    x += width + kGap;
  }
  (void)available;
}

void EnvTabBar::OnThemeChanged() {
  views::View::OnThemeChanged();
  Refresh();
}

void EnvTabBar::Refresh() {
  if (!manager_) {
    return;
  }
  RemoveAllChildViews();

  const std::string env_id = manager_->active_env_id();
  const std::vector<EnvManager::TabInfo> tabs = manager_->TabsForEnv(env_id);

  // Environment chip first: this bar replaces the window title, so it has to say which
  // environment the window is showing.
  auto* env_label = AddChildView(std::make_unique<views::Label>(
      u"\u25cf " + base::UTF8ToUTF16(env_id) + u" \u00b7 " +
      base::NumberToString16(static_cast<int>(tabs.size()))));
  env_label->SetEnabledColor(ui::kColorLabelForeground);
  env_label->SetFontList(
      gfx::FontList().DeriveWithSizeDelta(1).DeriveWithWeight(gfx::Font::Weight::MEDIUM));
  env_label->SetBorder(views::CreateEmptyBorder(gfx::Insets::VH(0, 6)));

  for (const EnvManager::TabInfo& tab : tabs) {
    const int index = tab.index;
    auto chip = std::make_unique<views::LabelButton>(
        base::BindRepeating(
            [](base::WeakPtr<EnvTabBar> self, int tab_index) {
              if (!self || !self->manager_) {
                return;
              }
              self->manager_->ActivateTabInEnv(self->manager_->active_env_id(),
                                               tab_index);
            },
            weak_factory_.GetWeakPtr(), index),
        Shorten(tab.title.empty() ? std::u16string(u"(untitled)") : tab.title));
    chip->SetMinSize(gfx::Size(60, kChipHeight));
    chip->SetMaxSize(gfx::Size(240, kChipHeight));
    chip->SetHorizontalAlignment(gfx::ALIGN_LEFT);
    chip->SetTooltipText(tab.title);
    chip->SetEnabledTextColors(
        tab.active ? ui::ColorVariant(ui::kColorLabelForeground)
                   : ui::ColorVariant(ui::kColorLabelForegroundSecondary));
    chip->SetBackground(tab.active ? views::CreateRoundedRectBackground(
                                         ui::kColorMenuItemBackgroundSelected, 6)
                                   : nullptr);
    views::LabelButton* chip_view = AddChildView(std::move(chip));

    auto* close = AddChildView(std::make_unique<views::LabelButton>(
        base::BindRepeating(
            [](base::WeakPtr<EnvTabBar> self, int tab_index) {
              if (!self || !self->manager_) {
                return;
              }
              self->manager_->CloseTabInEnv(self->manager_->active_env_id(), tab_index);
            },
            weak_factory_.GetWeakPtr(), index),
        u"\u00d7"));
    close->SetMinSize(gfx::Size(20, kChipHeight));
    close->SetTooltipText(u"\u5173\u95ed\u6807\u7b7e");

    LOG(ERROR) << "ENVTABBAR: chip tab=" << index << " title="
               << base::UTF16ToUTF8(chip_view->GetText()).substr(0, 24)
               << " active=" << tab.active;
  }

  auto* add = AddChildView(std::make_unique<views::LabelButton>(
      base::BindRepeating(
          [](base::WeakPtr<EnvTabBar> self) {
            if (!self || !self->manager_) {
              return;
            }
            std::string error;
            self->manager_->NewTab(self->manager_->active_env_id(), std::string(),
                                   &error);
          },
          weak_factory_.GetWeakPtr()),
      u"+"));
  add->SetMinSize(gfx::Size(28, kChipHeight));
  add->SetTooltipText(u"\u65b0\u5efa\u6807\u7b7e");

  LOG(ERROR) << "ENVTABBAR: refreshed env=" << env_id << " tabs=" << tabs.size()
             << " children=" << children().size();
  InvalidateLayout();
  DeprecatedLayoutImmediately();

  // Report the real rects on the next turn so a probe can click them.
  base::SingleThreadTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](base::WeakPtr<EnvTabBar> self) {
            if (!self) {
              return;
            }
            LOG(ERROR) << "ENVTABBAR: bar screen="
                       << self->GetBoundsInScreen().ToString();
            for (size_t index = 0; index < self->children().size(); ++index) {
              views::View* child = self->children()[index];
              LOG(ERROR) << "ENVTABBAR: child " << index << " screen="
                         << child->GetBoundsInScreen().ToString();
            }
          },
          weak_factory_.GetWeakPtr()));
}

BEGIN_METADATA(EnvTabBar)
END_METADATA

}  // namespace env
