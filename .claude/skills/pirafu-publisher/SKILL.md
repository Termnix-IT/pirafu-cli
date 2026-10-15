---
name: pirafu-publisher
description: pirafu CLIを使い、作品リポジトリの公開準備、Pages状態の確認、Pirafu2411 Playgroundへの登録・更新PR作成を支援する。作品自体の実装やUIデザインには使用しない。「作品を公開したい」「Playgroundに登録・掲載したい」「pirafu.yamlを作りたい」「Pagesの公開状態を確認したい」「登録PRを作りたい」といった依頼や、pirafuコマンドの実行・結果の解釈を求められたときに使う。
---

# Pirafu Publisher

Codex向けの`skills/pirafu-publisher`と同じ手順を、Claude Code向けに置き換えたSkillである。違いはCLIの探し方、コマンドの実行方法、PRの扱いだけで、判断基準と手順は変えていない。手順を変える場合は両方を更新する。

作品側の`pirafu.yaml`を原本とし、検証・状態照合・登録差分の生成はすべてCLIへ委ねる。Skill内でスキーマ、Cloudflare API、画像変換、Git操作、PR生成を再実装しない。

## 実行対象を確定する

- 作品ルートと適用される作業指示を読み、既存の設定・ビルド方法・Git差分を確認する。作品のスタックをNodeへ統一しない。
- CLIはPATH上の`pirafu`、利用者の指定、`PIRAFU_CLI_ROOT`から特定する。このSkillの実体がCLI checkoutの`.claude/skills/pirafu-publisher`にある場合は、3階層上のpyproject.tomlのproject.nameが`pirafu-cli`であることを確認して候補にできる。ローカルパスを作品メタデータや公開PR本文へ転記しない。
- 未インストールのcheckoutでは初回の依存導入として`uv sync --locked`を行い、以後は`uv run --project <CLIの絶対パス> --no-sync pirafu ...`を使う。configも作品側の絶対パスを指定する。CLIと作品の作業ディレクトリを混同しない。
- `--version`と必要なコマンドの`--help`で対応引数を確認する。このSkillは`apiVersion: 1`の結果を扱う。CLIが見つからなければ勝手な実装で代用せず、場所を確認する。
- Portalのowner/repoとbaseは利用者の指定または既存の承認から確定する。作品ID、Pages名、本番URL、公開リポジトリの可視性を推測して確定しない。

## 目的に合うCLIを実行する

業務コマンドの呼出しには`--json --non-interactive`を付け、プロセス終了コードとstdoutを別々に取得する。version/helpは通常のテキストとして読み、JSONとして解析しない。トークンを引数やファイルに書かず、認証情報の値・生の失敗ログを出力しない。

Claude Codeでは、WindowsではPowerShellツール、それ以外ではBashツールで実行する。

- **stdoutと終了コードの取得**：PowerShellでは`$out = <コマンド> 2>$null; $code = $LASTEXITCODE`、Bashでは`out=$(<コマンド> 2>/dev/null); code=$?`のように、stdoutと終了コードを別々に受け取る。
- **文字化けの防止**：Windowsで日本語を含むJSONが崩れる場合は、実行前に`[Console]::OutputEncoding = [Text.UTF8Encoding]::new()`を設定する。
- **長時間かかる呼出し**：`status --wait`のように2分を超え得る呼出しは、ツールのtimeoutを延ばすか、バックグラウンドで実行する。
- **出力の共有**：stdoutの生のJSONを、そのまま利用者への返答に貼らない。

| 目的 | 呼出しと判断 |
| --- | --- |
| 構成の診断 | `doctor --config <config> --json --non-interactive`。認証を照会できない場合は`--offline`でローカル診断し、オンライン確認済みと扱わない |
| 設定作成 | `init --config <config> ... --dry-run --json --non-interactive`で、検出候補・不足項目・生成内容を確認する。承認範囲内で実作成するときだけdry-runを外して`--yes`。既存設定を上書きしない |
| ローカル検査 | `check --config <config> --target local --json --non-interactive`。ビルドを自動実行するコマンドではない |
| 動作確認 | `preview --config <config> --dry-run --json --non-interactive`で計画を確認してから起動する。作品コードを実行する`--run-command --yes`はそのコマンドの承認範囲を確認する。返されたsessionとstopCommandで停止し、表示PIDを直接killしない |
| 公開準備 | `publish --config <config> --json --non-interactive`で成果物・Git連携・Pages設定を読み取り照合する。成功を新規デプロイ完了と表現しない。未連携なら管理画面の手動手順を案内する |
| 公開状態 | `status --config <config> --json --non-interactive`。必要なら期限を指定した`--wait`と`--expect-commit`を使い、現在の本番と最新試行を分けて読む |
| 登録・更新計画 | `register --config <config> --portal-repo <owner/repo> --base <base> --dry-run --json --non-interactive`。baseに存在するIDの変更には`--update`を付ける |

成果物不足は作品の既存ビルド手順で解決する。ビルドは作品コードの実行なので、コマンドと承認範囲を確認し、未知の複合コマンドを自動実行しない。未実装の`check --build`等を補って呼び出さない。

## JSONと終了コードを確認する

stdoutはJSONオブジェクト1件として解析し、apiVersionが整数1、commandが呼出し対象、outcomeがsuccess/no_change/planned/needs_action/errorのいずれかであることを確認する。JSON破損、複数JSON、未知の版・outcome、終了コードとの矛盾、必要フィールドの欠落は未確認として停止する。修復したJSONから適用へ進まず、値を含む生出力も共有しない。

| 終了コード・結果 | 次の行動 |
| --- | --- |
| 0 + success | 要求したコマンドが確認した範囲だけを成功とする。statusやpublishだけでアプリの操作確認完了とはしない |
| 0 + planned | 計画ができた状態。実際の公開・登録完了ではない |
| 0 + no_change | 追加変更なし。既存PRがあればそのURLを報告する。PRを増やさない |
| 5 + needs_action | nextActions、errors、不足情報を確認し、必要な利用者操作を示す。成功扱いせず、適用へ進まない |
| 4 | 認証・権限の確認を利用者に依頼する。チャットへ秘密情報を送らせない |
| 7 | ID衝突、変更された計画、base/head/PRの差異などを確認する。force pushや上書きで解消しない |
| 2 / 3 / 6 / 1 / 130 | 引数、資材、通信、内部異常、中断として内容を確認する。読み取りの一時障害は一度だけ再試行できる。状態が変わらなければ停止して未完了を報告する |

`--offline`は認証・本番URLの実状態を証明しない。Pages作品ではPages Read用の環境変数が必要で、ブラウザーのログインをCLIのAPI認証として扱わない。showcase登録はホスティング認証なしで進められる。unmanaged作品の`--confirm-external`は利用者が公開先を手動確認した場合だけ使い、Pagesの検査を迂回するためには使わない。

## 登録計画を提示して適用する

1. `readyToApply: true`、空でないplanHash、repo/base/branch、base/head SHA、変更前後のrecord、変更パス・容量・ハッシュ、既存PRを確認する。readyToApplyは真偽値trueを要求する。
2. 何を公開リポジトリへ送るか、PRとPagesプレビューが公開される可能性を説明する。既存の掲載・起動停止はCLIが維持する。停止解除や公開範囲の変更を紛れ込ませない。
3. 利用者の具体的な承認と継続承認を再利用する。新しい外部変更・本番公開・権限変更など、承認範囲を超える部分だけ確認する。Skillの起動やdry-runの成功を、外部変更の承認に置き換えない。
4. 承認対象と同じ引数に`--apply --yes --expect-plan <確認したplanHash>`を指定して実行する。dry-runは外す。`--update`等の条件を変える場合は再計画する。CLIが古い計画を拒否した場合は、新しい差分を確認し、承認対象と異なる変更は承認を得る。
5. PR作成後はURLと実行結果を確認する。Claude Codeのデスクトップアプリでは、PR連携ツール（`mcp__ccd_pr__get_status`）でそのPRがセッションに紐付いているか確認し、紐付いていなければ`mcp__ccd_pr__bind_pr`で紐付ける。CI・プレビューは現時点の状態を確認し、完了を待つための繰り返し確認はしない。マージは別の操作として依頼と承認範囲を守り、今回の登録成功から自動的に承認されたと解釈しない。

外部変更の途中で失敗したら、branchMayExist、branch、既存PRを手掛かりに同じ対象のdry-runで現在の計画を取り直す。成功していたpushやPRを重複作成せず、回復もCLIに任せる。破損・未知版のJSONしか得られない場合は適用を再試行しない。ghやCloudflare APIを直接呼んでCLIの検査・承認を迂回しない。

## 報告する

ローカル検証、既存Pages照合、アプリの操作確認、登録PR作成、PRマージを区別し、実際に完了した範囲と未完了の利用者操作を示す。API認証不足、手動Git接続、公開先不一致はそのまま伝える。Termnix-ITの既存ドメイン・AWSや内部管理環境への変更を、このワークフローの一部として行わない。内部作品は公開可能なshowcase素材と実環境を分ける。
