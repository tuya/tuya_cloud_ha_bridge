<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/tiankuosun/tuya_cloud_ha_bridge@main/custom_components/tuya_cloud_ha_bridge/brand/logo.png" alt="tuya_cloud_ha_bridge" width="240" />
</p>

# tuya_cloud_ha_bridge

tuya_cloud_ha_bridge 是一个 Home Assistant 自定义集成，通过涂鸦 Tuya Link 协议将 HA 设备桥接到涂鸦云平台，实现双向控制。

## 功能

- 通过涂鸦 OpenAPI 自动创建虚拟网关设备
- 基于 MQTT (Tuya Link) 实现云端实时双向状态同步
- 支持品类：灯 (light)、开关 (switch)、风扇 (fan)、空调 (climate)、窗帘 (cover)、加湿器 (humidifier)、除湿器 (dehumidifier)、扫地机 (vacuum)、热水器 (water_heater)、媒体播放器 (media_player)、摄像头 (camera)
- 自动设备发现与绑定
- 支持涂鸦 App 远程控制 HA 设备

## 安装

### HACS 安装（推荐）

1. 在 HACS 中添加本仓库作为自定义仓库
2. 搜索 "tuya_cloud_ha_bridge" 并安装
3. 重启 Home Assistant

**说明（图标）**：

- **HACS 仓库列表**里的小图标仍多依赖 CDN，自定义集成可能仍是占位图，见 [HACS 已知限制](https://github.com/hacs/integration/issues/5171)。
- **「设置 → 设备与服务」里的集成图标**：自 **Home Assistant Core 2026.3** 起，自定义集成才支持通过 `custom_components/<域名>/brand/icon.png` 在本机显示；**低于 2026.3 时会出现 “Icon not available”**，与是否 HACS 安装成功无关。请先升级 Core 至 **2026.3.0 或以上**并重启，再刷新浏览器缓存。
- 若本页顶部 Logo 不显示，请确认 GitHub 默认分支为 `main`、仓库为公开。

### 手动安装

1. 下载本仓库
2. 将文件复制到 `custom_components/tuya_cloud_ha_bridge/` 目录
3. 重启 Home Assistant

## 配置

1. 前往 **设置 > 设备与服务 > 添加集成**
2. 搜索 "tuya_cloud_ha_bridge"
3. 输入涂鸦云平台 API Key（从涂鸦开发者平台获取）
4. 按照提示完成配置

## 依赖

- **Home Assistant Core 2026.3.0+**（集成图标依赖 [Brands Proxy API 与本地 `brand/`](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)；更低版本无法在「设备与服务」中显示自定义品牌图）
- paho-mqtt 2.1.0（由集成 `manifest.json` 自动安装）

## 许可证

[MIT License](LICENSE)
