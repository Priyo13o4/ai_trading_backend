// @ts-nocheck

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient, type SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2.49.4";

type SupportedEvent = "account_deletion_otp";

interface DispatchRequest {
  event_type: SupportedEvent;
  user_id?: string;
}

interface UserContext {
  userId: string;
  recipientEmail: string;
}

interface OtpRow {
  otp_code: string;
  otp_expires_at: string;
}

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-webhook-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(status: number, body: Record<string, unknown>) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...corsHeaders,
      "Content-Type": "application/json",
    },
  });
}

function requireEnv(name: string): string {
  const value = Deno.env.get(name)?.trim();
  if (!value) {
    throw new Error(`Missing required secret: ${name}`);
  }
  return value;
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

class SmtpLineReader {
  private readonly conn: Deno.Conn;
  private buffer = "";
  private readonly chunk = new Uint8Array(1024);

  constructor(conn: Deno.Conn) {
    this.conn = conn;
  }

  async readLine(): Promise<string> {
    while (true) {
      const newlineIndex = this.buffer.indexOf("\n");
      if (newlineIndex !== -1) {
        const line = this.buffer.slice(0, newlineIndex + 1);
        this.buffer = this.buffer.slice(newlineIndex + 1);
        return line.replace(/\r?\n$/, "");
      }

      const bytesRead = await this.conn.read(this.chunk);
      if (bytesRead === null) {
        if (this.buffer.length > 0) {
          const remaining = this.buffer;
          this.buffer = "";
          return remaining;
        }
        throw new Error("SMTP connection closed unexpectedly");
      }

      this.buffer += decoder.decode(this.chunk.subarray(0, bytesRead));
    }
  }
}

async function writeAll(conn: Deno.Conn, payload: Uint8Array): Promise<void> {
  let offset = 0;
  while (offset < payload.length) {
    const written = await conn.write(payload.subarray(offset));
    offset += written;
  }
}

async function writeLine(conn: Deno.Conn, line: string): Promise<void> {
  await writeAll(conn, encoder.encode(`${line}\r\n`));
}

async function readSmtpReply(reader: SmtpLineReader): Promise<{ code: number; message: string }> {
  const lines: string[] = [];

  while (true) {
    const line = await reader.readLine();
    if (!line) {
      continue;
    }

    lines.push(line);

    if (/^\d{3}[ -]/.test(line) && line[3] === " ") {
      return {
        code: Number(line.slice(0, 3)),
        message: lines.join(" | "),
      };
    }
  }
}

async function expectSmtpCode(reader: SmtpLineReader, expected: number[], step: string): Promise<void> {
  const reply = await readSmtpReply(reader);
  if (!expected.includes(reply.code)) {
    throw new Error(`${step} failed with ${reply.code}: ${reply.message}`);
  }
}

function sanitizeHeader(value: string): string {
  return value.replace(/[\r\n]+/g, " ").trim();
}

function dotStuff(value: string): string {
  return value
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .split("\n")
    .map((line) => (line.startsWith(".") ? `.${line}` : line))
    .join("\r\n");
}

function buildMimeMessage(
  fromName: string,
  fromEmail: string,
  to: string,
  subject: string,
  text: string,
  html: string,
): string {
  const boundary = `pipfactor_${crypto.randomUUID().replace(/-/g, "")}`;
  const safeFromName = sanitizeHeader(fromName);
  const safeTo = sanitizeHeader(to);
  const safeSubject = sanitizeHeader(subject);

  const normalizedText = dotStuff(text || "");
  const normalizedHtml = dotStuff(html || "");

  return [
    `From: ${safeFromName} <${fromEmail}>`,
    `To: <${safeTo}>`,
    `Subject: ${safeSubject}`,
    `Date: ${new Date().toUTCString()}`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary=\"${boundary}\"`,
    "",
    `--${boundary}`,
    "Content-Type: text/plain; charset=UTF-8",
    "Content-Transfer-Encoding: 8bit",
    "",
    normalizedText,
    "",
    `--${boundary}`,
    "Content-Type: text/html; charset=UTF-8",
    "Content-Transfer-Encoding: 8bit",
    "",
    normalizedHtml,
    "",
    `--${boundary}--`,
  ].join("\r\n");
}

async function resolveUserContext(
  req: Request,
  body: DispatchRequest,
  admin: SupabaseClient,
  supabaseUrl: string,
  anonKey: string,
): Promise<UserContext> {
  const internalSecret = Deno.env.get("INTERNAL_WEBHOOK_SECRET")?.trim();
  const incomingSecret = req.headers.get("x-webhook-secret")?.trim();

  if (internalSecret && incomingSecret && incomingSecret === internalSecret) {
    if (!body.user_id) {
      throw new Error("user_id is required for internal dispatch requests");
    }

    const { data: userData, error: userError } = await admin.auth.admin.getUserById(body.user_id);
    if (userError || !userData?.user) {
      throw new Error("Could not resolve user for internal dispatch request");
    }

    const internalEmail = userData.user.email?.trim();
    if (!internalEmail) {
      throw new Error("Target user does not have an email address");
    }

    return {
      userId: body.user_id,
      recipientEmail: internalEmail,
    };
  }

  const authHeader = req.headers.get("Authorization") || "";
  if (!authHeader.startsWith("Bearer ")) {
    throw new Error("Missing bearer token");
  }

  const authClient = createClient(supabaseUrl, anonKey, {
    global: {
      headers: {
        Authorization: authHeader,
      },
    },
    auth: {
      persistSession: false,
      autoRefreshToken: false,
    },
  });

  const {
    data: { user },
    error: authError,
  } = await authClient.auth.getUser();

  if (authError || !user) {
    throw new Error("Unauthorized");
  }

  const email = user.email?.trim();
  if (!email) {
    throw new Error("Authenticated user has no email address");
  }

  return {
    userId: user.id,
    recipientEmail: email,
  };
}

async function loadActiveOtp(admin: SupabaseClient, userId: string): Promise<OtpRow> {
  const nowIso = new Date().toISOString();

  const { data, error } = await admin
    .from("account_deletion_requests")
    .select("otp_code, otp_expires_at")
    .eq("user_id", userId)
    .eq("verified", false)
    .gt("otp_expires_at", nowIso)
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle<OtpRow>();

  if (error) {
    throw new Error("Failed to fetch OTP request");
  }

  if (!data?.otp_code) {
    throw new Error("No active OTP request found");
  }

  return data;
}

function buildAccountDeletionEmail(otpCode: string, expiryIso: string) {
  const expiryDate = new Date(expiryIso);
  const expiryMinutes = Number.isFinite(expiryDate.getTime())
    ? Math.max(1, Math.ceil((expiryDate.getTime() - Date.now()) / 60000))
    : 10;

  return {
    subject: "PipFactor account deletion verification code",
    text: `Your PipFactor account deletion code is ${otpCode}. This code expires in about ${expiryMinutes} minute(s). If you did not request account deletion, please ignore this email.`,
    html: `
      <div style="font-family: Arial, sans-serif; line-height: 1.5; color: #111827;">
        <h2 style="margin: 0 0 12px 0;">Confirm Account Deletion</h2>
        <p style="margin: 0 0 12px 0;">Use the verification code below to confirm deletion of your PipFactor account:</p>
        <p style="font-size: 28px; font-weight: bold; letter-spacing: 4px; margin: 0 0 16px 0;">${otpCode}</p>
        <p style="margin: 0 0 12px 0;">This code expires in about <strong>${expiryMinutes} minute(s)</strong>.</p>
        <p style="margin: 0; color: #6b7280;">If you did not request this, you can ignore this email.</p>
      </div>
    `.trim(),
  };
}

async function sendZohoEmail(to: string, subject: string, text: string, html: string) {
  const hostname = Deno.env.get("ZOHO_SMTP_HOST")?.trim() || "smtp.zoho.com";
  const port = Number(Deno.env.get("ZOHO_SMTP_PORT") || "465");

  // SMTP login credentials (Zoho user)
  const username = requireEnv("ZOHO_EMAIL");
  const password = requireEnv("ZOHO_APP_PASSWORD");

  // Sender identity (must match an allowed mailbox/alias)
  const fromEmail = requireEnv("MAIL_FROM_EMAIL");

  const fromName = Deno.env.get("MAIL_FROM_NAME")?.trim() || "PipFactor";

  if (!Number.isFinite(port) || port <= 0) {
    throw new Error("Invalid ZOHO_SMTP_PORT");
  }

  if (port === 25 || port === 587) {
    throw new Error("ZOHO_SMTP_PORT must be 465 on hosted Supabase Edge Functions (ports 25 and 587 are blocked)");
  }

  let conn: Deno.Conn | null = null;
  try {
    conn = await Deno.connectTls({ hostname, port });

    const reader = new SmtpLineReader(conn);
    await expectSmtpCode(reader, [220], "SMTP greeting");

    await writeLine(conn, "EHLO pipfactor.com");
    await expectSmtpCode(reader, [250], "EHLO");

    await writeLine(conn, "AUTH LOGIN");
    await expectSmtpCode(reader, [334], "AUTH LOGIN");

    await writeLine(conn, btoa(username));
    await expectSmtpCode(reader, [334], "AUTH username");

    await writeLine(conn, btoa(password));
    await expectSmtpCode(reader, [235], "AUTH password");

    // Use alias sender for envelope MAIL FROM
    await writeLine(conn, `MAIL FROM:<${fromEmail}>`);
    await expectSmtpCode(reader, [250], "MAIL FROM");

    await writeLine(conn, `RCPT TO:<${to}>`);
    await expectSmtpCode(reader, [250, 251], "RCPT TO");

    await writeLine(conn, "DATA");
    await expectSmtpCode(reader, [354], "DATA");

    const mimeMessage = buildMimeMessage(
      fromName,
      fromEmail,
      to,
      subject,
      text,
      html,
    );

    await writeAll(conn, encoder.encode(`${mimeMessage}\r\n.\r\n`));
    await expectSmtpCode(reader, [250], "Message submission");

    await writeLine(conn, "QUIT");
    await expectSmtpCode(reader, [221], "QUIT");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`SMTP dispatch failed (${hostname}:${port}): ${message}`);
  } finally {
    if (conn) {
      try {
        conn.close();
      } catch {
        // no-op
      }
    }
  }
}

serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  if (req.method !== "POST") {
    return json(405, { success: false, error: "Method not allowed" });
  }

  let stage = "parse_request";
  try {
    const body = (await req.json()) as DispatchRequest;
    if (!body || body.event_type !== "account_deletion_otp") {
      return json(400, {
        success: false,
        error: "Unsupported event_type. Expected account_deletion_otp",
      });
    }

    console.log("account-deletion-mailer invocation", {
      event_type: body.event_type,
      has_authorization_header: Boolean(req.headers.get("Authorization")),
      has_internal_secret_header: Boolean(req.headers.get("x-webhook-secret")),
    });

    stage = "load_env";
    const supabaseUrl = requireEnv("SUPABASE_URL");
    const anonKey = requireEnv("SUPABASE_ANON_KEY");
    const serviceRoleKey =
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")?.trim() ||
      Deno.env.get("SUPABASE_SECRET_KEY")?.trim();
    if (!serviceRoleKey) {
      throw new Error("Missing required secret: SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY)");
    }

    stage = "create_admin_client";
    const admin = createClient(supabaseUrl, serviceRoleKey, {
      auth: {
        persistSession: false,
        autoRefreshToken: false,
      },
    });

    stage = "resolve_user_context";
    const userContext = await resolveUserContext(req, body, admin, supabaseUrl, anonKey);
    stage = "load_active_otp";
    const otpRow = await loadActiveOtp(admin, userContext.userId);

    stage = "build_email";
    const email = buildAccountDeletionEmail(otpRow.otp_code, otpRow.otp_expires_at);
    stage = "send_email";
    await sendZohoEmail(userContext.recipientEmail, email.subject, email.text, email.html);

    console.log("account-deletion-mailer success", {
      user_id: userContext.userId,
      recipient_domain: userContext.recipientEmail.split("@")[1] || "unknown",
    });

    return json(200, {
      success: true,
      event_type: body.event_type,
      recipient: userContext.recipientEmail,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    const stack = error instanceof Error ? error.stack : undefined;
    console.error("account-deletion-mailer failure", { stage, message, stack });
    return json(500, {
      success: false,
      stage,
      error: message,
    });
  }
});
