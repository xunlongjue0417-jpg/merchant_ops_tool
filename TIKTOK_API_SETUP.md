# TikTok Shop 官方 API：库存同步接入清单

这份清单只适用于店主自己的 TikTok Shop 店铺。不要把 TikTok 密码、浏览器 Cookie 或 App Secret 发到聊天里。

## 选什么 App

在 TikTok Shop Partner Center 完成 Developer Onboarding 后，创建 **Custom App**：

- Market：Malaysia
- Seller type：Local Seller
- Service category：TikTok Shop Seller（若控制台显示不同名称，选最接近的店铺／商品管理类别）
- Enable API：打开

Custom App 是给指定卖家使用的私有 App，不会公开上架。三家自己的店可以逐家授权。若未来给许多不同商家使用，才需要改走 Public App 和更严格审核。

官方说明：

- Create an App: https://partner.tiktokshop.com/docv2/page/create-your-app
- Seller developer onboarding: https://partner.tiktokshop.com/docv2/page/seller-developer-onboarding-onepager
- Publish custom App: https://partner.tiktokshop.com/docv2/page/publish-custom-app

## 创建时必须准备

1. **HTTPS Redirect URL**：卖家同意授权后 TikTok 把短期 `auth_code` 送到这里。正式版需要你控制的 HTTPS 地址；本机 `127.0.0.1` 不适合生产。
2. **HTTPS Webhook URL**：自动库存同步要接订单事件时需要。可先留空，只做手动／定时对账测试。
3. **App Key / App Secret**：创建后在 Partner Center 可见。App Secret 只能放在服务器环境变量或加密密钥库，绝不能放进网页 JavaScript、GitHub 或聊天记录。

## 申请哪些权限

到 `Partner Center > App & Service > Manage > Manage API`，只申请实际需要的范围。

最低目标：

- 读取店铺、订单和商品资料所需的当前范围；控制台会按 endpoint 显示要求。
- 写回库存：库存更新 endpoint 明确要求 `seller.product.write`。

不要申请买家资料、营销或非必要权限。平台的可申请范围会调整，以 Partner Center 当天显示的 endpoint requirement 为准。

官方资料：

- Access scopes: https://partner.tiktokshop.com/docv2/page/access-scope
- Inventory update API: https://partner.tiktokshop.com/docv2/page/6503068fc20ad60284b38858

## 每一家店怎么授权

1. Custom App 通过发布／注册审核后，在 App 页面复制 **Authorize link**。
2. 用该店的 Seller Center 帐号打开链接，确认授权范围。
3. TikTok 回跳到 Redirect URL，带回一次性的短期 `auth_code`。
4. 后端立即把 code 换成该店自己的 `access_token` 和 `refresh_token`，并安全保存。
5. 三家店各做一次。令牌不能跨店复用。

`auth_code` 短期有效且只能使用一次；前端页面不应接触 App Secret。官方 OAuth 说明：

- https://partner.tiktokshop.com/docv2/page/authorization-overview-202407
- https://partner.tiktokshop.com/docv2/page/authorization-guide-202309

## 上线顺序（不能跳）

1. Partner Center Sandbox 创建测试 Seller Shop。
2. 先读取测试商品与库存，不写入。
3. 对一个测试 SKU 发一次库存更新，确认 API 响应和 Seller Center 结果一致。
4. 测试重复事件、网络中断、429 限流、订单取消、退款、人工改库存。
5. 真实店先接一款商品、保留安全缓冲、只告警。
6. 连续对账正确后，才开启自动库存写回。

系统上线后，每次写入都必须保存：事件 ID、原库存、目标库存、店铺、商品 ID、SKU ID、API 响应和重试次数。重复事件必须幂等，不能重复扣库存。

Sandbox 官方说明：https://partner.tiktokshop.com/docv2/page/seller-center-development-shops
