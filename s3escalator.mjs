// services/s3escalator.mjs — S3 uploader (v3 SDK)
import "dotenv/config";
import express from "express";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";

const {
  PORT=8792, ALLOWED_ORIGINS="*",
  AWS_REGION="us-east-1", AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
  S3_BUCKET="dAisys-diary", S3_PREFIX="", UPLOADER_TOKEN=""
} = process.env;

const app = express();
app.use(express.json({ limit: "6mb" }));

app.use((req,res,next)=>{
  const allow = ALLOWED_ORIGINS==="*" ? "*" : (req.headers.origin || ALLOWED_ORIGINS);
  res.setHeader("Access-Control-Allow-Origin", allow);
  res.setHeader("Access-Control-Allow-Methods","GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","content-type, authorization, x-proxy-token");
  if (req.method==="OPTIONS") return res.sendStatus(200);
  next();
});

const s3 = new S3Client({ region: AWS_REGION, credentials: AWS_ACCESS_KEY_ID ? { accessKeyId: AWS_ACCESS_KEY_ID, secretAccessKey: AWS_SECRET_ACCESS_KEY } : undefined });

function today(){ return new Date().toISOString().slice(0,10); }

app.post("/tools/s3escalator", async (req,res)=>{
  try {
    const token = req.headers["x-proxy-token"] || "";
    if (UPLOADER_TOKEN && token !== UPLOADER_TOKEN) return res.status(401).json({ ok:false, error:"Bad token" });
    const { type="logs", path="", sender="unknown", fileBase64="", fileData="", file="" } = req.body || {};
    const folder = `${S3_PREFIX||""}${S3_PREFIX?"/":""}${type}/${sender}/${today()}`;
    const key = `${folder}/${path || "upload_"+Date.now()+".log"}`;
    const body = fileBase64 ? Buffer.from(fileBase64, "base64") : (fileData ? Buffer.from(String(fileData)) : Buffer.from(String(file||"")));
    await s3.send(new PutObjectCommand({ Bucket:S3_BUCKET, Key:key, Body: body }));
    res.json({ ok:true, bucket:S3_BUCKET, key });
  } catch (e) {
    res.status(500).json({ ok:false, error: String(e) });
  }
});

app.get("/healthz",(req,res)=>res.json({ ok:true }));
app.listen(Number(PORT), ()=>console.log(`[s3escalator] up on ${PORT} -> bucket=${S3_BUCKET}`));
