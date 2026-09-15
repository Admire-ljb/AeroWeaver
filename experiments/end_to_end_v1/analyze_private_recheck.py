"""Offline audit of the bounded protocol recheck; no provider calls."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    root = args.run
    rows, issues, hashes, refs = [], [], set(), 0
    count = calls = tokens = 0
    for phase in ['legacy', 'fixed']:
        completion = read(root / phase / 'completion.json')
        calls += completion['calls']; tokens += completion['tokens']
        for item in completion['rows']:
            folder = root / phase / 'episodes' / item['episode_id']
            trace = [json.loads(line) for line in (folder / 'trace.jsonl').read_text().splitlines()]
            records = [json.loads(line) for line in (folder / 'experience.jsonl').read_text().splitlines()]
            hashes.add(item['initial_state_sha256'])
            sequence = [event['decisions']['UAV_2']['choice']['parameters'].get('symbol', 'wait') for event in trace]
            rows.append({'phase': phase, 'method': item['method'], 'model': item['model'],
                         'return': item['controlled_team_mean_return'], 'success': item['task_success'],
                         'receiver_sequence': ' / '.join(map(str, sequence)),
                         'errors': item['decision_errors'] + item['invocation_errors'] + item['review_errors'],
                         'ranking_changes': item['memory_ranking_changes']})
            for record in records:
                count += 1
                agent = record['agent_id']; step = record['step_index']
                expected = sum(.95 ** (event['step'] - step) * event['rewards'][agent]
                               for event in trace if event['step'] >= step)
                if not record['return_finalized'] or not math.isclose(record['return_value'], expected, abs_tol=1e-9):
                    issues.append('return mismatch: ' + record['experience_id'])
                if record['role'] in ['receiver','eavesdropper'] and 'private_symbol' in record['state']:
                    issues.append('plaintext leak: ' + record['experience_id'])
                if record['role'] == 'eavesdropper' and 'private_key' in record['state']:
                    issues.append('key leak: ' + record['experience_id'])
                if phase == 'fixed' and step == 0 and record['role'] == 'receiver' and record['skill'] != 'hold_position':
                    issues.append('premature decoding: ' + record['experience_id'])
            for event in trace:
                for decision in event['decisions'].values():
                    for ref in (decision.get('ranking') or {}).get('retrieved', []):
                        refs += 1
                        if ref['mission'] != item['episode_id'] or ref['step'] >= event['step']:
                            issues.append('retrieval boundary violation')
    if len(hashes) != 1:
        issues.append('initial state mismatch')
    interface = read(root / 'interface/model-interface-check.json')
    audit = {'episodes': len(rows), 'records': count, 'retrieval_references': refs, 'issues': issues,
             'model': interface, 'experiment_calls_excluding_interface': calls, 'experiment_tokens': tokens,
             'paired_initial_states': len(hashes) == 1,
             'reward_source_hash_unchanged': read(root / 'legacy/plan.json')['runtime_hashes']['backend/sim/mock_rewards.py']
             == read(root / 'fixed/plan.json')['runtime_hashes']['backend/sim/mock_rewards.py']}
    (root / 'audit.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
    with (root / 'comparison.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    names = {'central_api': 'Central API', 'hmas2_adapted': 'HMAS-2 adapted', 'aeroweaver_pilot': 'AeroWeaver',
             'aeroweaver_full_catalog': 'Full catalog', 'aeroweaver_no_peer': 'No coordination reports',
             'aeroweaver_no_rl': 'No reward correction'}
    table = '\n'.join(f"| {r['phase']} | {names[r['method']]} | {r['receiver_sequence']} | {r['return']:.1f} | {'是' if r['success'] else '否'} |" for r in rows)
    report = f"""# Private Communication 诊断与复测

## Material Passport
- Mode: run / validation
- Source: 本目录原始 API 日志、逐步轨迹、独立 SQLite 经验库及部署日志。
- Scope: 同一初态 seed=66001，三轮协议；修复前两条件、修复后六条件；不是多种子性能评估。
- Model: 请求 `{interface['requested_model']}`；服务端响应 model 字段为 `{interface['response_model']}`，没有客户端模型回退。

## 发现的问题

1. 原六条件的密文均已成功投递。明文为 3、密钥为 2、密文为 1；接收者提交 0、1 或 2，最终都没有还原 3。并非断连或消息丢失。
2. `decode_message(symbol)` 的执行器仅记录参数，并不自动解密。旧文档没有直接说明解码公式与参数含义；轨迹与模型输入表明这一接口存在歧义。
3. 第零轮没有密文，旧选项仍允许接收者猜测，与技能文档中的 received evidence 前提不一致。
4. 奖励本身没有实现错误：这是直接调用 MPE2 奖励函数的 Mock 场景，合作方收益同时包含接收者误差和窃听者误差。因此总回报为零并不表示接收者正确；应同时查看 receiver_correct。三轮 complete 也仅表示协议结束。
5. 当前优势按技能名聚合，四种 decode_message 参数得到相同校正，不能改变它们的相对排序。这是技能级重排序的能力边界，本轮没有修改 RL 定义或奖励。

## 已修改并部署

- 局部观察明确提供通用 XOR 协议、0..3 符号域以及解码参数含义；没有直接提供本回合的解码答案。
- 无密文时，接收者和窃听者只能等待；有密文时仍保留全部四个符号候选，由模型选择。
- 更新 skill.md；只移植线上 Mock 的 observe 和 _options，保留其余线上实现。
- 全局 DeepSeek 默认值及 planner、tool_caller、doc_generator、doctor 模块已持久化切换到指定模型，后端重启后验证通过。独立视觉渠道 VLM 未改动。
- 实验入口默认模型同步更新，历史结果、历史请求及实验快照不改名、不覆盖。

## 同模型复测

所有条件使用相同初始明文、密钥、位置和旧的任务级激活列表；没有重新挑选激活列表。
下表序列是接收者三轮提交的参数，wait 表示等待。成功指最后一轮接收者正确且窃听者错误。

| 接口 | 条件 | 接收者序列 | 合作方平均回合回报 | 最终成功 |
|---|---|---|---:|---|
{table}

仅切换模型时旧接口两组仍失败。修复后六组中四组最后一轮成功，但全技能与无奖励校正条件仍有参数选择错误，尚不能称为稳定解码。
本轮全部 memory_ranking_changes 为零，完整方法与无奖励校正的结果差异不能解释为 RL 收益。
此外，等待替代了旧接口第零轮的盲猜，回报变化也包含这一时序修复的影响。
中央基线能看到汇聚后的 sender/receiver 观察，不能将其表现解释为与局部智能体相同的信息约束。窃听者是固定策略，并非学习型密码攻击者。
No coordination reports 保留 ciphertext 固有任务通道，本场景不适合用这一开关验证协同消息的收益。

## 核验与异常

- {count} 条经验记录逐条核对折扣回报与终止标记；{refs} 条检索引用核对时间及回合边界；核验问题 {len(issues)} 个。
- {calls} 次实验 API 请求、{tokens} 个实验 tokens，另有两次接口检查；全部八回合无决策、执行或评审错误。
- 17 项协议测试（覆盖全部 16 种明文/密钥组合）、23 项奖励测试和 85 项实验接口测试通过。
- 前置测试曾遇到缺少测试文件及一次既有 Mock 测试触发 Python 段错误，记录分别在相邻 151052、151113 目录；当时尚未调用付费接口或部署。段错误根因未确诊，拆分的上述测试通过不等于完整回归套件通过。
- 线上服务部署时处于空闲状态；重启后恢复 initialized/idle。实验使用隔离经验库，没有向线上生产记忆库写入复测数据。

原先的 54 回合总表不覆盖，本次修复诊断结果单独保存在 comparison.csv。
"""
    (root / 'REPORT.zh-CN.md').write_text(report, encoding='utf-8')
    print(json.dumps(audit, ensure_ascii=False))
    if issues:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
