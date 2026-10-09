# RTX 5060 Ti 8G 好价监控

一个适合放在 **GitHub Actions** 云端运行的小型价格监控。默认每 **5 分钟**执行一次，不需要你的电脑保持开机。

## 现在会做什么

- 搜索 **什么值得买** 的最新 RTX 5060 Ti 8G 优惠线索（能覆盖京东/拼多多/淘宝等很多爆料来源）
- 直接搜索 **京东** 商品列表，读取当前公开价格
- 过滤 `5060 非 Ti / 显存规格混杂 / 二手 / 拆机 / 矿卡` 等无关结果
- 什么值得买线索需在副标题、摘要或 SKU 选项中找到与具体 5060 Ti 显存规格绑定的价格；仅有标题或多规格最低价的线索标为低可信，不发提醒，也不计入新走势图样本
- 普通品牌实际抓取价 `≤ 2999` 元提醒
- 微星 / 华硕 / 技嘉等主流品牌 `≤ 3099` 元提醒
- 命中后立即通过 **飞书机器人 Webhook** 发消息
- 同一链接、同一价格默认 12 小时内不重复轰炸；若再降 30 元以上可再次提醒
- 通知状态保存在 `.watch_state.json`，只有真正发提醒时才提交一次状态，不会每 5 分钟制造 Git 提交

> 注意：GitHub Actions 的 cron 最小粒度可到 5 分钟，但 GitHub 不保证严格准点，繁忙时可能延迟。电商价格也可能因账号、地区、会员、券、库存而不同，下单前始终以结算页为准。

---

## 第一步：新建 GitHub 仓库

GitHub → `New repository`

推荐：
- Repository name：`5060ti-price-watch`
- Visibility：`Private`

把这个项目目录里的全部文件上传到仓库根目录。

目录结构应该是：

```text
5060ti-price-watch/
├─ .github/
│  └─ workflows/
│     └─ watch.yml
├─ tests/
│  └─ test_watch.py
├─ .gitignore
├─ .watch_state.json
├─ config.json
├─ requirements.txt
├─ watch.py
└─ README.md
```

---

## 第二步：添加飞书 Secret

仓库打开：

`Settings → Secrets and variables → Actions → New repository secret`

新增：

```text
Name: FEISHU_WEBHOOK
Secret: 你的飞书机器人 Webhook
```

**不要**把 Webhook 直接写进代码。

### 可选：提高“什么值得买”搜索成功率

再新增一个 Secret：

```text
Name: SMZDM_COOKIE
Secret: 你登录什么值得买后的 Cookie
```

不填也会尝试抓取；如果发现 SMZDM 经常 403/空结果，再加。

### 可选：提高京东搜索成功率

```text
Name: JD_COOKIE
Secret: 你的京东 Cookie
```

不填也会先尝试公开搜索页。

---

## 第三步：先测试飞书

进入：

`Actions → RTX 5060 Ti 8G Price Watch → Run workflow`

勾选：

```text
只测试飞书通知 = true
```

然后点 **Run workflow**。

几秒到一分钟后，飞书应该收到：

```text
【5060 Ti 8G 价格监控测试】
GitHub Actions → 飞书通知已打通。
```

---

## 第四步：自动运行

什么都不用再做。

工作流里已经配置：

```yaml
cron: '2-57/5 * * * *'
```

也就是每小时：

```text
02、07、12、17、22、27、32、37、42、47、52、57 分
```

执行一次。

特意避开 `00、05、10...`，是为了减少 GitHub Actions 整点拥堵。

---

## 修改价格阈值

编辑 `config.json`：

```json
"thresholds": {
  "normal": 2999,
  "good_brand": 3099
}
```

例如想更激进，主流品牌 3199 也提醒：

```json
"good_brand": 3199
```

---

## 如何额外盯某个具体商品链接

把商品链接加进 `config.json` 的 `direct_urls`。

示例：

```json
"direct_urls": [
  {
    "name": "微星 RTX 5060 Ti 8G 万图师",
    "platform": "京东",
    "url": "https://item.jd.com/xxxxxxxx.html",
    "title_regex": "5060\\s*ti.*8g"
  }
]
```

如果页面价格文本比较特殊，还可以加 `price_regex`：

```json
"price_regex": "券后[^0-9]{0,10}([0-9]{4})"
```

拼多多/淘宝经常使用动态页面、登录态和账号定向券，所以**直链 HTML 并不一定能读到 App 里的真实券后价**。这也是为什么本项目把“什么值得买最新爆料”作为跨平台发现入口之一。

---

## 为什么没有直接暴力爬淘宝/拼多多搜索页

淘宝、天猫、拼多多搜索页的反爬/登录/动态渲染变化很频繁；在 GitHub Actions 的共享云 IP 上尤其容易出现：

- 验证码
- 空白结果
- 账号/地区价格不同
- 推广 SKU 与实际 SKU 不一致
- 页面上看到的是活动价，但结算页条件不同

所以当前版本采用：

1. **SMZDM 新优惠发现**
2. **京东公开搜索页**
3. **你指定商品直链**

这个组合比把某个短期可用的 PDD 私有接口硬编码进去更稳定，也更容易维护。

---

## 日志怎么看

GitHub 仓库 → `Actions` → 点某一次运行 → `Check price and notify`

日志里会看到：

```text
[smzdm] 3 candidates
[jd] 5 candidates
CHECK 3099 <= 3099? 京东 | 微星 ...
  -> ALERT SENT
```

如果源站拦截，会看到：

```text
[smzdm] failed: ...
```

---

## 当前提醒规则

默认：

- 普通 RTX 5060 Ti 8G：`≤ ¥2999`
- 微星 / 华硕 / 技嘉：`≤ ¥3099`
- 二手、拆机、矿卡、16GB、非 Ti 等自动排除
- 同价 12 小时内不重复提醒
- 再降至少 30 元可以再次提醒

这些都可以在 `config.json` 改。

