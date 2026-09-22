# 开发者镜像（个性化 DDI）

iOS 17 及以上使用 **个性化 Developer Disk Image**。挂载依据是 `Restore/BuildManifest.plist` 中的 `ApChipID` 与 `ApBoardID`，与「按系统版本选择一份 `DeveloperDiskImage.dmg`」不是同一套逻辑。

实现位于 [`backend_function/ddi_manager.py`](../backend_function/ddi_manager.py)。

## 本地目录

| 路径 | 说明 |
|:-----|:-----|
| `runtime/devimages/` | 运行时下载与缓存（默认 `DEVIMAGES_DIR`） |
| `IOSPrechecker/devimages/` | 本机手动放置的镜像（可选；也会参与匹配） |
| `IOSPrechecker/executable/` | 从 `utils` 解压的 `ios` / `ios.exe`（不提交版本库） |

上游镜像来源：[doronz88/DeveloperDiskImage](https://github.com/doronz88/DeveloperDiskImage) 仓库 `main` 分支下的 `PersonalizedImages/Xcode_iOS_DDI_Personalized/`。

## 挂载流程（摘要）

1. 设备上已挂载开发者镜像 → 直接成功。
2. 读取芯片身份：已安装 `pymobiledevice3` 时查询；否则可能从 go-ios `image auto` 的 `findIdentity` 报错中解析。
3. 在本地 `runtime/devimages`（及本机 `IOSPrechecker/devimages`）中查找身份匹配的 `**/Restore`，仅硬挂匹配项。
4. 无匹配项 → 下载上游 `BuildManifest.plist`；清单含本机身份后，再下载该身份对应的 `Image.dmg` 与 trustcache，写入 `runtime/devimages/Xcode_iOS_DDI_Personalized/Restore`。
5. 仍失败 → 尝试 bundled go-ios `image auto`（旧二进制可能仍指向 `ddi-15F31d`）。
6. 最后 → 可选 `pymobiledevice3 mounter auto-mount`（需安装 `pymobiledevice3`）。

已有且身份匹配的本地镜像不会重复下载。上游清单不含该芯片身份时，不会下载载荷。

## 诊断

- HTTP：`POST /api/image/auto`，请求体 `{"udid":"<设备 UDID>"}`。
- 日志：`runtime/logs/ios_app.log`（搜索 `ApChipId`、`BoardId`、`DDI`）。
- 单测：`python -m unittest tests.test_ddi_manager`

## 可选依赖

```bash
pip install "pymobiledevice3>=4.0.0"
```

用于 `query-personalization-identifiers` 与 `mounter auto-mount` 兜底。
