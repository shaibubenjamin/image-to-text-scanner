package com.qex.scanner;

import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import okhttp3.MediaType;
import okhttp3.MultipartBody;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public class ProcessingActivity extends AppCompatActivity {

    private static final String BASE_URL = BuildConfig.BASE_URL;
    private static final MediaType JPEG = MediaType.parse("image/jpeg");

    private TextView tvStatus, tvDetail;
    private ProgressBar progressBar;
    private final OkHttpClient client = new OkHttpClient.Builder()
        .callTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
        .build();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private int jobId = -1;
    private boolean polling = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_processing);

        tvStatus   = findViewById(R.id.tvStatus);
        tvDetail   = findViewById(R.id.tvDetail);
        progressBar = findViewById(R.id.progressBar);

        ArrayList<String> paths = getIntent().getStringArrayListExtra("pages");
        if (paths == null || paths.isEmpty()) {
            finish();
            return;
        }

        tvStatus.setText("Uploading " + paths.size() + " page(s)…");
        uploadPages(paths);
    }

    private void uploadPages(ArrayList<String> paths) {
        executor.execute(() -> {
            try {
                MultipartBody.Builder builder = new MultipartBody.Builder()
                    .setType(MultipartBody.FORM);

                for (String path : paths) {
                    File f = new File(path);
                    builder.addFormDataPart("page", f.getName(),
                        RequestBody.create(f, JPEG));
                }

                Request request = new Request.Builder()
                    .url(BASE_URL + "/api/scan-upload")
                    .post(builder.build())
                    .build();

                try (Response response = client.newCall(request).execute()) {
                    String body = response.body() != null ? response.body().string() : "";
                    if (!response.isSuccessful()) throw new Exception("Upload failed: " + body);

                    JSONObject json = new JSONObject(body);
                    jobId = json.getInt("id");
                    polling = true;

                    mainHandler.post(() -> {
                        tvStatus.setText("Extracting text…");
                        tvDetail.setText("Processing your questionnaire");
                        startPolling();
                    });
                }

            } catch (Exception e) {
                mainHandler.post(() -> {
                    tvStatus.setText("Upload failed");
                    tvDetail.setText(e.getMessage());
                    Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show();
                });
            }
        });
    }

    private void startPolling() {
        executor.execute(this::poll);
    }

    private void poll() {
        if (!polling || jobId < 0) return;

        try {
            Request request = new Request.Builder()
                .url(BASE_URL + "/api/status/" + jobId)
                .build();

            try (Response response = client.newCall(request).execute()) {
                String body = response.body() != null ? response.body().string() : "{}";
                JSONObject json = new JSONObject(body);

                String status = json.getString("status");
                int pct = json.optInt("progress_pct", 0);

                mainHandler.post(() -> {
                    progressBar.setProgress(pct);
                    tvDetail.setText(pct + "% complete");

                    if (status.equals("review_pending") || status.equals("completed")) {
                        polling = false;
                        // Open review page in browser
                        String url = BASE_URL + "/results/" + jobId + "/review";
                        Intent i = new Intent(this, ResultActivity.class);
                        i.putExtra("url", url);
                        i.putExtra("job_id", jobId);
                        startActivity(i);
                        finish();

                    } else if (status.equals("failed")) {
                        polling = false;
                        String err = json.optString("error_message", "Unknown error");
                        tvStatus.setText("Extraction failed");
                        tvDetail.setText(err);

                    } else {
                        // Still processing — poll again in 2.5s
                        mainHandler.postDelayed(this::startPolling, 2500);
                    }
                });
            }

        } catch (Exception e) {
            mainHandler.postDelayed(this::startPolling, 4000);
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        polling = false;
        executor.shutdown();
    }
}
