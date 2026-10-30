
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return new Response("Market Monitor Bot is running.");
    }

    // One-time Telegram webhook setup
    if (request.method === "GET" && url.pathname === "/setup") {
      if (
        !env.SETUP_KEY ||
        url.searchParams.get("key") !== env.SETUP_KEY
      ) {
        return new Response("Forbidden", { status: 403 });
      }

      if (
        !env.TELEGRAM_BOT_TOKEN ||
        !env.TELEGRAM_WEBHOOK_SECRET
      ) {
        return new Response("Missing Telegram settings", {
          status: 500
        });
      }

      const result = await fetch(
        `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/setWebhook`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: `${url.origin}/telegram`,
            secret_token: env.TELEGRAM_WEBHOOK_SECRET,
            allowed_updates: ["message"]
          })
        }
      );

      const data = await result.json();

      return Response.json(data, {
        status: result.ok ? 200 : 502
      });
    }

    if (request.method !== "POST" || url.pathname !== "/telegram") {
      return new Response("Not found", { status: 404 });
    }

    if (
      !env.TELEGRAM_WEBHOOK_SECRET ||
      request.headers.get("X-Telegram-Bot-Api-Secret-Token") !==
        env.TELEGRAM_WEBHOOK_SECRET
    ) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad request", { status: 400 });
    }

    const message = update.message;

    if (!message?.text || !message.chat) {
      return new Response("OK");
    }

    const chatId = String(message.chat.id);
    const senderId = String(message.from?.id ?? "");
    const command = message.text.trim().split(/\s+/)[0].split("@")[0];

    if (
      message.chat.type !== "private" ||
      chatId !== String(env.TELEGRAM_CHAT_ID) ||
      senderId !== String(env.TELEGRAM_CHAT_ID)
    ) {
      return new Response("OK");
    }

    if (command !== "/bablo") {
      return new Response("OK");
    }

    async function reply(text) {
      const response = await fetch(
        `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: chatId, text })
        }
      );

      if (!response.ok) {
        console.error("Telegram reply failed:", response.status);
      }
    }

    const parts = String(env.GITHUB_REPOSITORY || "").split("/");
    const owner = parts[0];
    const repo = parts[1];

    if (
      parts.length !== 2 ||
      !owner ||
      !repo ||
      !env.GITHUB_TOKEN ||
      !env.GITHUB_REF ||
      !env.TELEGRAM_BOT_TOKEN
    ) {
      await reply("Ошибка настройки: проверь переменные Worker.");
      return new Response("OK");
    }

    try {
      const response = await fetch(
        `https://api.github.com/repos/${owner}/${repo}/actions/workflows/monitor.yml/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.GITHUB_TOKEN}`,
            Accept: "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json"
          },
          body: JSON.stringify({ ref: env.GITHUB_REF })
        }
      );

      if (response.status === 204) {
        await reply("🚀 Команда принята! Запускаю сканер рынка.");
      } else {
        console.error(
          "GitHub dispatch failed:",
          response.status,
          (await response.text()).slice(0, 500)
        );

        await reply(
          `Не удалось запустить сканер (HTTP ${response.status}). Проверь GitHub Token и workflow.`
        );
      }
    } catch (error) {
      console.error("Dispatch error:", error);
      await reply("Ошибка связи с GitHub. Попробуй ещё раз.");
    }

    return new Response("OK");
  }
};
