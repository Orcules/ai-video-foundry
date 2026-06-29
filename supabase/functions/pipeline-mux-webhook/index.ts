import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const encoder = new TextEncoder();

async function hmacSha256(key: Uint8Array, message: Uint8Array): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, message);
  return new Uint8Array(sig);
}

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  const ab = encoder.encode(a);
  const bb = encoder.encode(b);
  let diff = 0;
  for (let i = 0; i < ab.length; i++) {
    diff |= ab[i] ^ bb[i];
  }
  return diff === 0;
}

async function verifyMuxSignature(
  rawBody: string,
  signatureHeader: string,
  secret: string,
): Promise<boolean> {
  // Mux signature format: t=<timestamp>,v1=<signature>
  const parts: Record<string, string> = {};
  for (const part of signatureHeader.split(",")) {
    const [key, ...rest] = part.split("=");
    parts[key.trim()] = rest.join("=");
  }

  const timestamp = parts["t"];
  const v1Signature = parts["v1"];
  if (!timestamp || !v1Signature) return false;

  // Check 5-minute tolerance
  const ts = parseInt(timestamp, 10);
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > 300) return false;

  // Message to sign: <timestamp>.<raw_body>
  const message = `${timestamp}.${rawBody}`;
  const keyBytes = encoder.encode(secret);
  const msgBytes = encoder.encode(message);
  const expectedSig = toHex(await hmacSha256(keyBytes, msgBytes));

  return timingSafeEqual(expectedSig, v1Signature);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const rawBody = await req.text();

  // Verify Mux signature
  const signatureHeader = req.headers.get("mux-signature") ?? "";
  const webhookSecret = Deno.env.get("MUX_PIPELINE_WEBHOOK_SECRET") ?? "";

  if (!webhookSecret) {
    console.error("MUX_PIPELINE_WEBHOOK_SECRET not configured");
    return new Response("Server misconfigured", { status: 500 });
  }

  const valid = await verifyMuxSignature(rawBody, signatureHeader, webhookSecret);
  if (!valid) {
    console.error("Invalid Mux signature");
    return new Response("Invalid signature", { status: 401 });
  }

  // Parse event
  let event: {
    type: string;
    data: {
      id: string;
      passthrough?: string;
      playback_ids?: Array<{ id: string }>;
    };
  };
  try {
    event = JSON.parse(rawBody);
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const eventType = event.type;

  // Only handle asset.ready and asset.errored
  if (eventType !== "video.asset.ready" && eventType !== "video.asset.errored") {
    return new Response(JSON.stringify({ ignored: true, type: eventType }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Extract job_id from passthrough
  let jobId: string;
  try {
    const passthrough = JSON.parse(event.data.passthrough ?? "{}");
    jobId = passthrough.job_id;
  } catch {
    console.error("Failed to parse passthrough:", event.data.passthrough);
    return new Response(JSON.stringify({ error: "invalid passthrough" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  if (!jobId) {
    console.error("No job_id in passthrough");
    return new Response(JSON.stringify({ error: "missing job_id" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Create Supabase client with service role key
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  const supabase = createClient(supabaseUrl, supabaseKey);

  if (eventType === "video.asset.ready") {
    const playbackId = event.data.playback_ids?.[0]?.id;
    const assetId = event.data.id;

    if (!playbackId) {
      console.error("No playback_id in asset.ready event for job", jobId);
      return new Response(JSON.stringify({ error: "no playback_id" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Query the job
    let { data: job, error } = await supabase
      .from("video_jobs")
      .select("id, status, output")
      .eq("id", jobId)
      .single();

    if (error || !job) {
      console.error("Job not found:", jobId, error);
      return new Response(JSON.stringify({ error: "job not found" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Check if job is completed with mux_status uploading
    if (job.status !== "completed" || job.output?.mux_status !== "uploading") {
      // Wait 5s and retry once (handles fast Mux processing)
      console.log("Job not ready yet, waiting 5s...", {
        jobId,
        status: job.status,
        muxStatus: job.output?.mux_status,
      });
      await sleep(5000);

      const retry = await supabase
        .from("video_jobs")
        .select("id, status, output")
        .eq("id", jobId)
        .single();

      job = retry.data;
      error = retry.error;

      if (error || !job) {
        console.error("Job not found on retry:", jobId);
        return new Response(JSON.stringify({ error: "job not found on retry" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (job.status !== "completed" || job.output?.mux_status !== "uploading") {
        console.log("Job still not ready after retry, returning 200 for fallback", {
          jobId,
          status: job.status,
          muxStatus: job.output?.mux_status,
        });
        return new Response(
          JSON.stringify({ deferred: true, reason: "job not ready" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
    }

    // Fetch actual static rendition filename from Mux API
    const muxTokenId = Deno.env.get("MUX_TOKEN_ID") ?? "";
    const muxTokenSecret = Deno.env.get("MUX_TOKEN_SECRET") ?? "";
    let mp4Name = "highest.mp4"; // default for static_renditions API

    if (muxTokenId && muxTokenSecret) {
      try {
        const assetResp = await fetch(
          `https://api.mux.com/video/v1/assets/${assetId}`,
          {
            headers: {
              Authorization: "Basic " + btoa(`${muxTokenId}:${muxTokenSecret}`),
            },
          },
        );
        if (assetResp.ok) {
          const assetData = await assetResp.json();
          const rendFiles = assetData?.data?.static_renditions?.files ?? [];
          const readyFile = rendFiles.find((f: any) => f.status === "ready");
          if (readyFile?.name) {
            mp4Name = readyFile.name;
          }
        }
      } catch (e) {
        console.error("Failed to fetch Mux asset for rendition name:", e);
      }
    }

    // Merge Mux data into output
    const updatedOutput = {
      ...job.output,
      mux_status: "ready",
      final_stream_url: `https://stream.mux.com/${playbackId}.m3u8`,
      final_mp4_url: `https://stream.mux.com/${playbackId}/${mp4Name}`,
      final_playback_id: playbackId,
      final_asset_id: assetId,
    };

    const { error: updateError } = await supabase
      .from("video_jobs")
      .update({ output: updatedOutput })
      .eq("id", jobId);

    if (updateError) {
      console.error("Failed to update job:", jobId, updateError);
      return new Response(JSON.stringify({ error: "update failed" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    console.log("Updated job with Mux ready status:", {
      jobId,
      playbackId,
      assetId,
    });
  } else if (eventType === "video.asset.errored") {
    // Get current output and merge mux_status: failed
    const { data: job, error } = await supabase
      .from("video_jobs")
      .select("id, output")
      .eq("id", jobId)
      .single();

    if (error || !job) {
      console.error("Job not found for error event:", jobId, error);
      return new Response(JSON.stringify({ error: "job not found" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    const updatedOutput = {
      ...job.output,
      mux_status: "failed",
    };

    const { error: updateError } = await supabase
      .from("video_jobs")
      .update({ output: updatedOutput })
      .eq("id", jobId);

    if (updateError) {
      console.error("Failed to update job with error status:", jobId, updateError);
    }

    console.log("Updated job with Mux error status:", { jobId });
  }

  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
