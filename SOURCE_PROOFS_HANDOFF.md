# 源凭证交接说明（SOURCE_PROOFS 交接）

本说明面向青湾河口站夜班的采集、发布与离线复核三方，解释源凭证模式下证据如何交接。
业务契约见 `SOURCE_PROOFS.md`，可运行演示见 `python3 -B -m samples.source_proofs`。

## 角色与信任边界

- 门户可信入口：`ScriptedPortal.source_anchor(observer, start, end)`。
  它从门户已生成并固定的记录中读取锚点，不经过普通交付流，交付事件的漏送、增帧、改值、
  错绑（`omit`、`edit`）都无法改变已签发的锚点。
- 普通交付流：`poll` 排出的 `Delivery`（含 `SourceReceipt`）一律视为不可信，必须核验后才能接纳。
- 导出的证据包：`archive.export_source_segment` 产生的 JSON 包同样不可信，
  复核时只依赖调用方另存的可信锚点。

## 采集端如何把已验源凭证交给发布

1. 开班时调用 `sessions.opening.start(..., source_end=end)` 固定半开区间 `[start_seq, source_end)`、
   站点、观察身份和通道条件；条件在整班不可变。
2. 门户产生全班源记录后，调用 `portal.emit_source_receipt(observer, start, end)`。
   门户按观察身份和固定条件从实际源记录生成凭证：区间每个位置都有一个带随机盐的叶子，
   有帧位置绑定 `RawFrame` 五字段（含首次出处），空位置只有盐化摘要。
   根承诺对站点、观察身份、规范化条件、区间、凭证号和全部叶子求哈希，
   同时认证“绑定关系”和“实收原帧与门户匹配集合恰好一致”。
3. 采集端在凭证随交付到达前（或到达时），通过可信入口 `portal.source_anchor(...)`
   另存同一锚点。锚点是可 JSON 往返的独立对象，与交付材料分开保管。
4. `sessions.driver.step` 收到 `SourceReceipt` 时：
   - 用另存的可信锚点核对凭证内嵌锚点；无对应锚点即拒绝；
   - 重算每个叶子的盐化摘要并核对根承诺；
   - 用 `RawFrame` 五字段把凭证帧集合与库内实收原帧做**恰好相等**比较。
   缺帧、增帧、改值、错绑、普通 V1 凭证或锚点不符都抛 `ValueError`，
   该交付位置不推进，此前已提交的位置和原帧保留（漏送例停在位置 2，原帧 14、11 保留）。
5. 已验凭证与其交付位置在同一事务入账。同一凭证原样重投不新增证据；
   后续冲突凭证、以及向已证空位凭空补帧都会被拒绝，已证集合不可改变。
6. 封存只接受开班绑定的截止点。`archive.seal` 把已验凭证、锚点、隐藏承诺材料、
   原帧、无匹配区间和质量成果在同一事务提交；缺少已验全班凭证时返回无成果号和全班缺口。
   提交失败回滚，保全既有成果与本班待封存状态，同截止点重试沿用原成果号（`shift-<run_id>`）。

## 复核方如何取得独立锚点并离线核验

1. 门户可用时，复核方自行调用可信入口 `source_anchor(observer, start, end)` 另存锚点。
   该通道与交付材料独立，编辑交付内容不会影响锚点。
2. 门户停用、数据库关闭后，复核方仅凭成果号领取授权子区间：
   `bundle = archive.export_source_segment(db, result_id, start, end)`，
   要求 `[start,end)` 是班内非空子区间；未知成果抛 `KeyError`，
   损坏证据抛 `StoredEvidenceError`，非法区间抛 `ValueError`，可读的 V1 成果返回 `None`。
3. 断库断门户后调用
   `frames, no_match = archive.verify_source_segment(bundle, trusted_anchor, start, end)`。
   函数没有门户和数据库参数，核验顺序为：
   - 区间合法且落在班内，源身份（站点/观察/条件）与可信锚点一致；
   - 重算全部叶子的盐化摘要并用可信锚点的根承诺核对，区间内帧逐字段比对；
   - 区间内有帧位置必须全部交付且无增项，空位合并为相邻 `Interval`。
   成功才返回按源序的 `RawFrame` 元组和无匹配区间元组；任何缺项、增项、改值、错绑或格式错误
   都抛 `ValueError`，不交付部分结果。
4. 隐私边界：区间外位置只随包携带盐化叶子（`seq/kind/salt/digest`），
   不含原值、不含开启材料、也不含可直接枚举低取值域整数的无盐指纹；
   完整的隐藏承诺材料允许随包携带，由盐保证不可离线枚举原值。

## 运行清单

- 核验：`python3 -B check_project.py`（全部 unittest，含原 18 项回归与源凭证新增用例，失败非零退出）。
- 演示：`python3 -B -m samples.source_proofs`，实际运行漏送拒绝、完整封存、
  停源重开领包、断库复核，并保留 V1 完整/漏送事故对照。
