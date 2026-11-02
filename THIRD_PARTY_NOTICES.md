# Third-Party Notices

The BlueBean license applies only to original BlueBean materials and does not relicense third-party components. Each third-party component remains subject to its own license and notices.

| Component | Where used | License / notice |
|---|---|---|
| Chromium | Browser engine and Chromium modification patches | Chromium BSD-style license; see `LICENSES/CHROMIUM-BSD-3-CLAUSE.txt`, the upstream `chrome://credits` page, and notices in the installed browser. |
| DeepSeek Harness (DSH) | Agent workbench and runtime bundled in the Windows installer | MIT; copyright and terms belong to DeepSeek. See [upstream repository](https://github.com/deepseek-ai/deepseek-harness) and its `LICENSE`. |
| Python | Bundled runtime in the Windows installer | Python Software Foundation License; see `runtime/LICENSE.txt` in the installed application. |
| Python packages | Bundled runtime | Each package retains its own license and metadata under `runtime/Lib/site-packages`; see `runtime/bundled-packages.json` for versions. |
| DSH/Electron dependencies | Bundled workbench runtime | Individual licenses and notices are retained in the installed DSH application under `resources/app.asar.unpacked` and its package metadata. |
| Social platform icons | `lan_seats/assets/platform-icons` | See `lan_seats/assets/platform-icons/NOTICE.md`; platform names and marks belong to their respective owners. |

The release installer bundles components under different licenses. Review the included notices for each component; the BlueBean license does not grant rights to third-party trademarks or replace upstream licenses.
