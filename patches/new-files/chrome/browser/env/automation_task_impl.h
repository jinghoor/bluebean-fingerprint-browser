// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_BROWSER_ENV_AUTOMATION_TASK_IMPL_H_
#define CHROME_BROWSER_ENV_AUTOMATION_TASK_IMPL_H_

#include <memory>
#include <string>
#include <vector>

#include "base/functional/callback.h"
#include "base/memory/weak_ptr.h"
#include "chrome/browser/env/automation_task.mojom.h"
#include "mojo/public/cpp/bindings/pending_receiver.h"
#include "mojo/public/cpp/bindings/receiver.h"

namespace content {
class RenderFrameHost;
}

namespace env {

// The typed seam between the automation transport and the in-browser executor (design doc §31/§32).
// The transport only ever sees `mojom::AutomationTask`; it never touches WebContents,
// RenderFrameHosts or environment lookup, so a different transport (Mojo channel, named pipe,
// socket) can be swapped in without changing the executor.
class AutomationTaskImpl : public mojom::AutomationTask {
 public:
  // Resolves the environment's frame; supplied by EnvManager so this class stays free of
  // environment bookkeeping.
  using FrameResolver =
      base::RepeatingCallback<content::RenderFrameHost*(const std::string& env_id)>;

  AutomationTaskImpl(mojo::PendingReceiver<mojom::AutomationTask> receiver,
                     FrameResolver resolve_frame);
  AutomationTaskImpl(const AutomationTaskImpl&) = delete;
  AutomationTaskImpl& operator=(const AutomationTaskImpl&) = delete;
  ~AutomationTaskImpl() override;

  // mojom::AutomationTask:
  void RunTask(const std::string& env_id,
               std::vector<mojom::OpPtr> ops,
               RunTaskCallback callback) override;
  void ClickTrusted(const std::string& env_id,
                    const std::vector<std::string>& selectors,
                    ClickTrustedCallback callback) override;

 private:
  mojo::Receiver<mojom::AutomationTask> receiver_;
  FrameResolver resolve_frame_;
  base::WeakPtrFactory<AutomationTaskImpl> weak_factory_{this};
};

}  // namespace env

#endif  // CHROME_BROWSER_ENV_AUTOMATION_TASK_IMPL_H_
