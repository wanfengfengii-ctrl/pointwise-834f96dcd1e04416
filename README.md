# 青湾河口班次采集与离线质检

接入员从测站门户接纳固定通道集合下的原始整数帧和交付凭证。值班员按源序号核对完整性和计量口径。封存成功后，质检员可在停源状态下用成果号复核读数、出处、空段和统计。

## 运行

需要 Python 3.11 及其 SQLite 标准库。从项目根目录执行以下命令。`check_project.py` 由运行平台物化。

```sh
python3 -B check_project.py
python3 -B -m samples.day_shift
```

可以直接运行完整 unittest 套件。

```sh
python3 -B -m unittest discover -s tests
```

## 接口

- `database.open_database(path)` 打开本地库并准备表结构。连接由调用者关闭。
- `registry.initialize_defaults(db)` 返回默认修订标识，保留已登记内容。`registry.register(db, station, name, channels, calibrations)` 用 `Channel` 和 `ChannelCalibration` 登记口径。同站同名修订内容固定。
- `sessions.opening.start(db, portal, station, revision_id, condition, start_seq)` 返回班次号。条件是不可变的非空站内通道集合。注册返回的首位置表示观察资格。
- `sessions.driver.step(db, portal, run_id)` 拉取当前可用交付，返回已确认交付数、新增帧数、新增凭证数、交付位置和班次状态。源序号可乱序，交付位置按句柄递增。异常保留此前已接纳的前缀。
- `evidence.coverage.coverage(db, run_id, start, end)` 核算半开区间的已证、未证和已证无匹配范围。缺少凭证的位置继续待核。
- `archive.seal(db, run_id, end, portal=portal)` 封存从班次起点到 `end` 的半开区间。结果带缺口或固定成果号。完成封存后可以用同一截止点重试。活动班次使用原观察句柄所属的门户实例。门户释放异常时，`release_error` 随已经提交的成果号返回。
- `archive.get(db, result_id)` 返回已验证的冻结制品，包括班次区间、固定修订、原帧、凭证、无匹配区间和逐帧及通道统计。未知号抛出 `KeyError`，损坏制品抛出 `StoredEvidenceError`。

原帧重投按源序号和原始内容比较，保留首次出处。凭证包含条件、观察句柄、半开区间和出处。只有凭证证明的范围才用于确认无匹配。校准系数和限值接受整数、有理数字符串或 `Fraction`，浮点输入会被拒绝。换算按 `原值 × 比例 + 偏置`，限值等值算在限内，通道均值包含全部有效读数，零样本均值为空。

## 源凭证模式（SOURCE_PROOFS）

在 V1 之外新增全班源凭证模式，原入口、旧成果和全部原回归保持可用。契约见 `SOURCE_PROOFS.md`，交接说明见 `SOURCE_PROOFS_HANDOFF.md`。

- `sessions.opening.start(..., source_end=end)` 固定半开区间 `[start_seq, source_end)`；省略 `source_end` 时完全沿用 V1。
- `evidence.receipts.SourceReceipt(coverage, material)` 包装原 `CoverageReceipt` 和可 JSON 往返的证据材料。`ScriptedPortal.emit_source_receipt(observer, start, end)` 从门户实际源记录生成全班凭证并经 `Delivery` 排队；`ScriptedPortal.source_anchor(observer, start, end)` 是独立可信入口，返回含 `station`、`observer`、`condition`、`range`、`receipt_id`、非空 `commitment` 的锚点。
- 接纳时以独立另存的可信锚点核齐实收原帧：缺帧、增帧、改值、错绑、无锚点或普通 V1 凭证都在 `step` 中抛 `ValueError`，不推进交付位置，已提交前缀和原帧保留。
- 源凭证模式只接受开班绑定的截止点封存；凭证、锚点、隐藏承诺与成果同次提交，失败保全先前状态。
- `archive.export_source_segment(db, result_id, start, end)` 导出授权非空子区间证据包；可读的 V1 成果经此入口返回 `None`，未知成果抛 `KeyError`，损坏证据抛 `StoredEvidenceError`，非法区间抛 `ValueError`。
- `archive.verify_source_segment(bundle, trusted_anchor, start, end)` 无门户和数据库参数，凭另存可信锚点离线核验身份、完整匹配和空位，返回 `(frames, no_match)`；区间外只携带盐化承诺，不泄露原值、开启材料或无盐指纹。
- 运行演示：`python3 -B -m samples.source_proofs`（漏送拒绝、完整封存、停源重开领包、断库复核，并保留 V1 事故对照）。

## 样例

样例在临时库中初始化测站并登记人工修订 R1。门户先发盐度 3 号帧，随后发 1 号帧及重投。封存先返回 [0,2) 缺口，收到凭证后完成。停源重开可领取 1 和 4/3 g/L，均值 7/6 g/L，无匹配区间为 [0,1)、[2,3)、[4,5)。另一班次仅观察盐度，门户提供覆盖两条浊度源记录的盐度空凭证，离线统计显示零条盐度读数和空均值。

`samples.portal_fixture.ScriptedPortal` 以显式推进产生连续源序号。观察注册、帧交付、凭证交付和历史留存分别控制。`read_history` 返回指定条件下实得的原帧、完整覆盖凭证、不可取区间和仍未产生的区间。`ESTUARY_REPLAY` 是从 98 起的一段可手算源输入。
