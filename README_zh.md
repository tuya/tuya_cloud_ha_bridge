# tuya_cloud_ha_bridge

`tuya_cloud_ha_bridge` 是一个 Home Assistant 自定义集成，用于将 Home Assistant 中的设备同步到涂鸦 App。集成会在涂鸦云侧创建一个虚拟网关，并通过 Tuya Link MQTT 实现云端双向状态同步，这样就可以在涂鸦 App 中完成设备控制、自动化和语音联动。

English documentation: [`README.md`](README.md)

## 功能特性

- 在 Home Assistant 中通过配置流创建涂鸦虚拟网关
- 基于 Tuya Link MQTT 实现云端实时双向状态同步
- 支持在涂鸦 App 中扫描二维码绑定虚拟网关
- 支持在 App 的网关面板中继续添加 Home Assistant 子设备
- 支持的典型实体类型包括：
  `light`、`switch`、`fan`、`climate`、`cover`、`humidifier`、`vacuum`、`water_heater`

## 环境准备

开始前请确认：

- Home Assistant 已正常运行
- 已准备好涂鸦云平台的 `API Key`
- 手机已安装支持扫码绑定的涂鸦 App，建议 App 版本不低于 `7.6.0`
- 如果选择 `HACS 安装`，请确保 Home Assistant 可以联网并访问 GitHub
- 如果选择 `自定义安装`，请确保你可以访问 Home Assistant 的 `config` 目录

## 安装方式

### 方式一：通过 HACS 安装

#### 场景 A：Home Assistant 尚未集成 HACS

HACS 并不是 Home Assistant 默认自带的组件，它是由社区维护的第三方扩展商店。如果你的 Home Assistant 里还没有 HACS，可以按下面步骤先完成 HACS 安装和初始化。

##### 1. 确认前置条件

- Home Assistant 已经安装并可正常运行
- 你拥有一个可用的 GitHub 账号，后续需要用于授权验证
- 你能够访问 Home Assistant 的文件系统，例如已经安装 `Advanced SSH & Web Terminal` 或 `File Editor`

##### 2. 通过脚本安装 HACS

1. 进入 Home Assistant 终端
2. 执行以下命令安装 HACS：

```bash
wget -O - https://get.hacs.xyz | bash -
```

3. 等待脚本执行完成
4. 脚本执行完成后，重启 Home Assistant

##### 3. 在界面中集成 HACS

重启完成后，HACS 还不会直接出现在左侧菜单栏，需要手动在 Home Assistant 界面中完成集成：

1. 点击左下角 `设置`
2. 进入 `设备与服务`
3. 点击右下角 `添加集成`
4. 搜索 `HACS` 并点击进入
5. 勾选所有声明选项
6. 页面会显示一个 8 位验证码
7. 点击页面中的 GitHub 授权链接
8. 跳转到 GitHub 后，输入验证码并完成授权

##### 4. 确认 HACS 已启用

完成授权后，HACS 会出现在 Home Assistant 左侧菜单栏中。确认 HACS 可以正常打开后，再继续执行下方“场景 B”的插件安装步骤。

如果你需要 HACS 安装的更详细图文教程，建议自行在网上搜索对应版本的 Home Assistant / HACS 安装指南。

#### 场景 B：Home Assistant 已经集成 HACS

1. 打开 Home Assistant 的 `HACS`
2. 进入自定义仓库管理页面，添加自定义仓库
3. 仓库地址填写：
   `https://github.com/tuya/tuya_cloud_ha_bridge.git`
4. 类型选择：`Integration`

![在 HACS 中添加自定义仓库](./images/hacs-repo.png)

5. 添加成功后，在 HACS 中搜索 `tuya_cloud_ha_bridge`

![在 HACS 中下载 tuya_cloud_ha_bridge](./images/hacs-intergation.png)
6. 重启 Home Assistant

### 方式二：自定义安装

1. 克隆或下载本仓库
2. 将目录 `custom_components/tuya_cloud_ha_bridge` 复制到 Home Assistant 的 `config/custom_components/` 下
3. 重启 Home Assistant

如果你希望在本机直接执行复制操作，可以使用仓库中的脚本：

```bash
./scripts/install_to_ha_custom_components.sh /path/to/homeassistant/config
```

例如：

```bash
./scripts/install_to_ha_custom_components.sh "/Volumes/homeassistant/config"
```

## 配置步骤

### 第 1 步：添加集成

1. 进入 Home Assistant
2. 打开 `设置 > 设备与服务`
3. 点击 `添加集成`
4. 搜索 `tuya_cloud_ha_bridge`
![在 HA 中搜索插件](./images/ha-bridage-1.png)
5. 打开集成配置向导

### 第 2 步：输入 API Key

1. 按向导提示打开涂鸦 API Key 获取页面：<https://tuya.ai/>
2. 从涂鸦云平台复制 `API Key`
3. 回到 Home Assistant，在输入框中粘贴 API Key

![在 HA API Key 输入](./images/ha-bridage-2.png)

4. 点击 `提交`

提交后，集成会：

- 在涂鸦云侧创建虚拟网关
- 建立临时 MQTT 连接
- 生成用于绑定网关的二维码

### 第 3 步：使用涂鸦 App 扫码绑定网关

1. 在 Home Assistant 配置页查看绑定二维码
2. 打开涂鸦 App 并扫描二维码

![在 HA 二维码扫描](./images/ha-bridage-3.png)

3. 完成 App 侧的网关添加流程
4. 返回 Home Assistant 页面后，务必点击 `提交`

![在 HA 集成结束](./images/ha-bridage-4.png)

> 注意：如果扫码后直接关闭页面而没有点击 `提交`，虚拟网关可能无法在 App 中正常使用。

### 第 4 步：在 App 中添加子设备

绑定完成后，请进入涂鸦 App 的虚拟网关面板，在网关下继续添加要同步的 Home Assistant 子设备。

## 常见问题

### 1. 搜索不到集成

- 确认插件目录已复制到 `config/custom_components/tuya_cloud_ha_bridge`
- 确认复制完成后已重启 Home Assistant
- 确认目录名和 `manifest.json` 中的 `domain` 一致

### 2. API Key 无法通过校验

- 请确认填写的是涂鸦云平台提供的 `API Key`
- 请确认该 API Key 所属区域受当前集成支持

### 3. 扫码后网关不可用

- 请确认扫码完成后回到 Home Assistant 页面点击了 `提交`
- 请确认 Home Assistant 所在网络能够访问涂鸦 MQTT 服务

## 目录说明

- `custom_components/tuya_cloud_ha_bridge`：Home Assistant 自定义集成目录
- `images`：README 文档中使用的安装和配置流程截图
- `scripts/install_to_ha_custom_components.sh`：将插件复制到本地 Home Assistant `custom_components` 目录的辅助脚本

## License

[MIT License](LICENSE)
