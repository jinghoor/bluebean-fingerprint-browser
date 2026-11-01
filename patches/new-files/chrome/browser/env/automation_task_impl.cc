// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "chrome/browser/env/automation_task_impl.h"

#include <utility>

#include "base/json/json_reader.h"
#include "base/json/json_writer.h"
#include "base/logging.h"
#include "base/strings/string_number_conversions.h"
#include "base/strings/utf_string_conversions.h"
#include "base/time/time.h"
#include "chrome/browser/env/automation_server.h"
#include "content/browser/renderer_host/render_widget_host_impl.h"
#include "content/public/browser/render_frame_host.h"
#include "content/public/browser/render_widget_host.h"
#include "content/public/browser/render_widget_host_view.h"
#include "third_party/blink/public/common/input/web_mouse_event.h"

namespace env {
namespace {

// Turns the typed ops back into the JSON op list the executor consumes. The executor itself is
// shared with the file-channel `task` command, so both transports behave identically.
base::ListValue OpsToJson(const std::vector<mojom::OpPtr>& ops) {
  base::ListValue list;
  for (const mojom::OpPtr& op : ops) {
    base::DictValue entry;
    const std::string& kind = op->kind;
    if (kind == "click") {
      entry.Set("click", op->selector);
    } else if (kind == "type") {
      base::DictValue type;
      type.Set("selector", op->selector);
      type.Set("text", op->text);
      entry.Set("type", std::move(type));
    } else if (kind == "read") {
      entry.Set("read", op->selector.empty() ? "body" : op->selector);
      entry.Set("limit", op->limit);
    } else if (kind == "observe") {
      entry.Set("observe", true);
      entry.Set("limit", op->limit);
    } else {
      entry.Set("error", "unknown kind " + kind);
    }
    list.Append(std::move(entry));
  }
  return list;
}

}  // namespace

AutomationTaskImpl::AutomationTaskImpl(
    mojo::PendingReceiver<mojom::AutomationTask> receiver,
    FrameResolver resolve_frame)
    : receiver_(this, std::move(receiver)),
      resolve_frame_(std::move(resolve_frame)) {}

AutomationTaskImpl::~AutomationTaskImpl() = default;

void AutomationTaskImpl::ClickTrusted(const std::string& env_id,
                                      const std::vector<std::string>& selectors,
                                      ClickTrustedCallback callback) {
  // Trusted input stays in the browser process: the renderer only reports where the element is,
  // and the click is forwarded through the renderer host's own input pipeline, so the page sees
  // `isTrusted: true`. CDP's equivalent measured ~0.6 ms/click (design doc §33); this skips the
  // CDP layer entirely.
  const base::TimeTicks started = base::TimeTicks::Now();
  content::RenderFrameHost* frame = resolve_frame_.Run(env_id);
  content::RenderWidgetHost* widget =
      frame ? frame->GetRenderWidgetHost() : nullptr;
  content::RenderWidgetHostView* view = widget ? widget->GetView() : nullptr;
  if (!frame || !widget || !view) {
    mojom::TaskResultPtr result = mojom::TaskResult::New();
    result->ok = false;
    result->error = "no frame for environment " + env_id;
    std::move(callback).Run(std::move(result));
    return;
  }

  base::ListValue wanted;
  for (const std::string& selector : selectors) {
    wanted.Append(selector);
  }
  std::string selector_json;
  base::JSONWriter::Write(wanted, &selector_json);
  const std::u16string script = base::UTF8ToUTF16(
      "(() => { const sels = " + selector_json +
      "; const out = sels.map((sel) => {"
      " const el = document.querySelector(sel);"
      " if (!el) return null;"
      " const r = el.getBoundingClientRect();"
      " return [r.left + r.width / 2, r.top + r.height / 2]; });"
      " return JSON.stringify({points: out,"
      " dpr: window.devicePixelRatio || 1}); })()");

  const float device_scale = view->GetDeviceScaleFactor();
  const size_t wanted_count = selectors.size();
  content::RenderWidgetHostImpl* host =
      content::RenderWidgetHostImpl::From(widget);
  frame->ExecuteJavaScriptInIsolatedWorld(
      script,
      base::BindOnce(
          [](base::TimeTicks began, content::RenderWidgetHostImpl* host_ptr,
             float scale, size_t count, ClickTrustedCallback done,
             base::Value value) {
            mojom::TaskResultPtr result = mojom::TaskResult::New();
            result->ms = (base::TimeTicks::Now() - began).InMillisecondsF();
            if (!host_ptr || !value.is_string() || value.GetString().empty()) {
              result->ok = false;
              result->error = "no rects";
              std::move(done).Run(std::move(result));
              return;
            }
            std::optional<base::Value> page =
                base::JSONReader::Read(value.GetString(), base::JSON_PARSE_RFC);
            if (!page || !page->is_dict()) {
              result->ok = false;
              result->error = "unreadable rects";
              std::move(done).Run(std::move(result));
              return;
            }
            const base::ListValue* points = page->GetDict().FindList("points");
            int clicked = 0;
            if (points) {
              for (const base::Value& item : *points) {
                if (!item.is_list() || item.GetList().size() < 2) {
                  continue;
                }
                const double x = item.GetList()[0].GetIfDouble().value_or(0);
                const double y = item.GetList()[1].GetIfDouble().value_or(0);
                const gfx::PointF position(static_cast<float>(x) * scale,
                                           static_cast<float>(y) * scale);
                const base::TimeTicks now = base::TimeTicks::Now();
                blink::WebMouseEvent press(
                    blink::WebInputEvent::Type::kMouseDown, position, position,
                    blink::WebMouseEvent::Button::kLeft, 1, 0, now);
                blink::WebMouseEvent release(
                    blink::WebInputEvent::Type::kMouseUp, position, position,
                    blink::WebMouseEvent::Button::kLeft, 1, 0, now);
                host_ptr->ForwardMouseEvent(press);
                host_ptr->ForwardMouseEvent(release);
                ++clicked;
                mojom::OpResultPtr op_result = mojom::OpResult::New();
                op_result->ok = true;
                op_result->detail =
                    "trusted " + base::NumberToString(static_cast<int>(x)) +
                    "," + base::NumberToString(static_cast<int>(y));
                result->results.push_back(std::move(op_result));
              }
            }
            result->ok = clicked > 0;
            if (!result->ok) {
              result->error = "no element found";
            }
            LOG(ERROR) << "ENVTRUSTED: clicked=" << clicked << " of " << count
                       << " ms=" << result->ms;
            std::move(done).Run(std::move(result));
          },
          started, host, device_scale, wanted_count, std::move(callback)),
      /*world_id=*/1);
}

void AutomationTaskImpl::RunTask(const std::string& env_id,
                                 std::vector<mojom::OpPtr> ops,
                                 RunTaskCallback callback) {
  const size_t op_count = ops.size();
  content::RenderFrameHost* frame = resolve_frame_.Run(env_id);
  if (!frame) {
    mojom::TaskResultPtr result = mojom::TaskResult::New();
    result->ok = false;
    result->error = "no frame for environment " + env_id;
    std::move(callback).Run(std::move(result));
    return;
  }
  base::ListValue json_ops = OpsToJson(ops);
  const base::TimeTicks started = base::TimeTicks::Now();
  RunOpsOnFrame(
      frame, std::move(json_ops),
      base::BindOnce(
          [](base::TimeTicks began, size_t count, RunTaskCallback done,
             base::Value value) {
            mojom::TaskResultPtr result = mojom::TaskResult::New();
            result->ms = (base::TimeTicks::Now() - began).InMillisecondsF();
            if (!value.is_string()) {
              result->ok = false;
              result->error = "executor returned no page result";
              std::move(done).Run(std::move(result));
              return;
            }
            std::optional<base::Value> page =
                base::JSONReader::Read(value.GetString(), base::JSON_PARSE_RFC);
            if (!page || !page->is_dict()) {
              result->ok = false;
              result->error = "unreadable page result";
              std::move(done).Run(std::move(result));
              return;
            }
            const base::DictValue& dict = page->GetDict();
            if (const std::string* url = dict.FindString("url")) {
              result->page_url = *url;
            }
            result->ok = true;
            if (const base::ListValue* list = dict.FindList("results")) {
              for (const base::Value& item : *list) {
                mojom::OpResultPtr op_result = mojom::OpResult::New();
                op_result->ok =
                    !item.is_dict() || !item.GetDict().contains("error");
                if (item.is_dict()) {
                  std::string detail;
                  base::JSONWriter::Write(item.GetDict(), &detail);
                  op_result->detail = detail;
                }
                result->results.push_back(std::move(op_result));
              }
            }
            LOG(ERROR) << "ENVMOJO: RunTask ops=" << count
                       << " ms=" << result->ms << " url=" << result->page_url;
            std::move(done).Run(std::move(result));
          },
          started, op_count, std::move(callback)));
}

}  // namespace env
