# Quick Start 🚀

1. サーバーに[BOT](https://discord.com/oauth2/authorize?client_id=1342526334529835048&permissions=280576&integration_type=0&scope=bot)を招待
2. サーバーまたはBOTのDMで`/register`コマンドを使用する（通知先チャンネルを省略するとコマンドを実行したチャンネルに通知）
3. 登録を解除する場合は`/unregister`コマンドから対象を選択する

# Manual Setup (Self hosted)
1. ルートディレクトリに`config.ini`を作成
```ini
[DISCORD]
TOKEN = ここにBOTのトークンを記載
```
2. main.pyを起動
 （適宜`pip install discord`などで足りないモジュールをインストール）
