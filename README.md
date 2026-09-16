# 商家利润与库存工具

这是第一阶段的本地工具：不需要 TikTok 密码、不开服务器、不会改动线上商品。

## 现在可用

1. 临时使用：双击 `启动工具.cmd`，打开 `http://127.0.0.1:8765`。不需要更改 Windows 的 PowerShell 执行策略。
2. 想免去每次启动：双击一次 `设置开机后台运行.cmd`。它会使用 Windows 的“启动”文件夹，不需要管理员权限，也不会出现黑色窗口。以后登录 Windows 后会自动后台运行；把 `http://127.0.0.1:8765` 收藏为浏览器书签即可。
2. 上传 TikTok Shop 导出的 **Income** 与 **All Orders** 两份 `.xlsx`。
3. 工具以 `Order ID` 合并：Income 的 `Total settlement amount` 是订单净收入，Orders 的实际商品数量用于扣成本。
4. 对每个实际售出的 TikTok 平台 `SKU ID` 填一次成本。成本可有生效日。
5. 数量不一致、找不到订单、缺成本时，一律标记 `需核对`，不显示虚假的利润。

`Seller SKU` 空白不影响工具。成本的主键是 TikTok 自动生成的平台 `SKU ID`；商品名称和变种只作为给人看的提示。

## 库存同步：当前状态

已实现本地库存映射与**模拟扣库存**，用来验证一个共用库存池、各店安全缓冲和目标可售库存的计算。它还没有连接 TikTok 生产环境，绝不会在未经授权时改你的真实库存。

真实写回库存之前必须完成：

1. TikTok Shop Partner Center 开发者入驻；
2. Malaysia / Local Seller 的 **Custom App**；
3. API 启用、HTTPS redirect URL、每家店各自 OAuth 授权；
4. 申请并获批实际所需的产品写入范围（库存更新 API 的文档列出 `seller.product.write`）；
5. 在 Sandbox 做订单、取消、重试、限流、库存对账测试。

不要用 Selenium、浏览器脚本、Cookie 或店铺密码模拟操作 Seller Centre。只使用官方 OAuth 与 API。

官方资料：

- https://partner.tiktokshop.com/docv2/page/create-your-app
- https://partner.tiktokshop.com/docv2/page/authorization-overview-202407
- https://partner.tiktokshop.com/docv2/page/access-scope
- https://partner.tiktokshop.com/docv2/page/6503068fc20ad60284b38858
- https://partner.tiktokshop.com/docv2/page/seller-center-development-shops

## 已知边界

- 利润结果是 `Total settlement amount - 实际未退货商品成本`。不要再次扣平台费、运费或达人佣金，因为它们已包含在结算净额。
- 若一个订单有多个商品，工具只计算**整单利润**。若要拆到每件商品，必须先选定分摊规则。
- 工具保存在本机 `data/state.json` 的成本不加密；不要把这个目录上传到公开仓库。

## Render 测试部署

本项目可部署为单用户测试网站。Render 使用根目录的 `Dockerfile` 自动安装 Python 依赖并启动服务。

在 Render 创建 **Web Service** 后：

1. 选择本私有仓库；
2. 环境选择 Docker；
3. 在 Environment 中新增 `APP_ACCESS_PASSWORD`，设为一串长且唯一的密码；
4. 部署后，打开 Render 提供的 HTTPS 网址，浏览器会先要求输入用户名和密码。用户名固定为 `merchant`。

Render 免费实例会休眠，且重启后本地上传文件、成本记录和下载报告会丢失。因此它只适合验证线上操作和 TikTok OAuth 技术流程，不能作为多人或税务资料的正式长期储存。
