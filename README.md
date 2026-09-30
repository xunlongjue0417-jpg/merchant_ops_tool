# 商家多平台利润核算工具

这是报表驱动的利润核算工具：不需要平台密码，不连接或修改线上店铺。

## 现在可用

1. 临时使用：双击 `启动工具.cmd`，打开 `http://127.0.0.1:8765`。不需要更改 Windows 的 PowerShell 执行策略。
2. 想免去每次启动：双击一次 `设置开机后台运行.cmd`。它会使用 Windows 的“启动”文件夹，不需要管理员权限，也不会出现黑色窗口。以后登录 Windows 后会自动后台运行；把 `http://127.0.0.1:8765` 收藏为浏览器书签即可。
2. 上传 TikTok Shop 导出的 **Income** 与 **All Orders** 两份 `.xlsx`。
3. 工具以 `Order ID` 合并：Income 的 `Total settlement amount` 是订单净收入，Orders 的实际商品数量用于扣成本。
4. 对每个实际售出的 TikTok 平台 `SKU ID` 填一次成本。成本可有生效日。
5. 数量不一致、找不到订单、缺成本时，一律标记 `需核对`，不显示虚假的利润。

也可以在“填写商品成本”区域上传 CSV、TSV 或 XLSX 商品成本表。建议栏位为：`Platform`、`SKU`、`Product Name`、`Unit Cost`、`Currency`、`Effective Date` 和 `Note`。当前 TikTok 行会立即用于计算；Shopee、eBay、Lazada、Amazon 行会先独立保存，等对应平台导入适配器启用后再使用，避免把不同平台的同名 SKU 混算。

`Seller SKU` 空白不影响工具。成本的主键是 TikTok 自动生成的平台 `SKU ID`；商品名称和变种只作为给人看的提示。

## 已知边界

- 利润结果是 `Total settlement amount - 实际未退货商品成本`。不要再次扣平台费、运费或达人佣金，因为它们已包含在结算净额。
- 若一个订单有多个商品，工具只计算**整单利润**。若要拆到每件商品，必须先选定分摊规则。
- 工具保存在本机 `data/state.json` 的成本不加密；不要把这个目录上传到公开仓库。

## Render 测试部署

本项目可部署为报表测试网站。Render 使用根目录的 `Dockerfile` 自动安装 Python 依赖并启动服务。

在 Render 创建 **Web Service** 后：

1. 选择本私有仓库；
2. 环境选择 Docker；
3. 如需限制访问，可在 Environment 中新增 `APP_ACCESS_PASSWORD`，设为一串长且唯一的密码；
4. 部署后打开 Render 提供的 HTTPS 网址即可使用报表核算功能。

Render 免费实例会休眠，且重启后本地上传文件、成本记录和下载报告会丢失。因此它适合功能验证，不适合作为正式长期财务资料储存。
