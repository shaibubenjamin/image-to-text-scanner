package com.qex.scanner;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

public class ScanActivity extends AppCompatActivity {

    private final List<File> scannedPages = new ArrayList<>();
    private PageAdapter adapter;
    private TextView tvPageCount;
    private Uri currentPhotoUri;
    private File currentPhotoFile;

    private final ActivityResultLauncher<Uri> cameraLauncher =
        registerForActivityResult(new ActivityResultContracts.TakePicture(), success -> {
            if (success && currentPhotoFile != null) {
                scannedPages.add(currentPhotoFile);
                adapter.notifyItemInserted(scannedPages.size() - 1);
                updatePageCount();
            }
        });

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_scan);

        tvPageCount = findViewById(R.id.tvPageCount);
        RecyclerView rvPages = findViewById(R.id.rvPages);
        Button btnScanPage = findViewById(R.id.btnScanPage);
        Button btnExtract  = findViewById(R.id.btnExtract);

        adapter = new PageAdapter(scannedPages);
        rvPages.setLayoutManager(new LinearLayoutManager(this, LinearLayoutManager.HORIZONTAL, false));
        rvPages.setAdapter(adapter);

        updatePageCount();

        btnScanPage.setOnClickListener(v -> requestCameraAndScan());

        btnExtract.setOnClickListener(v -> {
            if (scannedPages.isEmpty()) {
                Toast.makeText(this, "Scan at least one page first.", Toast.LENGTH_SHORT).show();
                return;
            }
            // Pass page file paths to ProcessingActivity
            ArrayList<String> paths = new ArrayList<>();
            for (File f : scannedPages) paths.add(f.getAbsolutePath());
            Intent intent = new Intent(this, ProcessingActivity.class);
            intent.putStringArrayListExtra("pages", paths);
            startActivity(intent);
        });
    }

    private void requestCameraAndScan() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                new String[]{Manifest.permission.CAMERA}, 100);
            return;
        }
        launchCamera();
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        super.onRequestPermissionsResult(req, perms, results);
        if (req == 100 && results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED)
            launchCamera();
        else
            Toast.makeText(this, "Camera permission required", Toast.LENGTH_SHORT).show();
    }

    private void launchCamera() {
        try {
            File dir = new File(getCacheDir(), "camera");
            if (!dir.exists()) dir.mkdirs();
            currentPhotoFile = File.createTempFile(
                "page_" + (scannedPages.size() + 1) + "_", ".jpg", dir
            );
            currentPhotoUri = FileProvider.getUriForFile(
                this, getPackageName() + ".fileprovider", currentPhotoFile
            );
            cameraLauncher.launch(currentPhotoUri);
        } catch (IOException e) {
            Toast.makeText(this, "Could not start camera: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    private void updatePageCount() {
        int n = scannedPages.size();
        tvPageCount.setText(n == 0
            ? "No pages scanned yet"
            : n + " page" + (n == 1 ? "" : "s") + " scanned");
    }

    // ── Thumbnail RecyclerView adapter ──────────────────────────────────────

    static class PageAdapter extends RecyclerView.Adapter<PageAdapter.VH> {
        private final List<File> pages;
        PageAdapter(List<File> pages) { this.pages = pages; }

        @Override
        public VH onCreateViewHolder(ViewGroup parent, int viewType) {
            View v = LayoutInflater.from(parent.getContext())
                .inflate(R.layout.item_page_thumb, parent, false);
            return new VH(v);
        }

        @Override
        public void onBindViewHolder(VH h, int pos) {
            File f = pages.get(pos);
            Bitmap bm = BitmapFactory.decodeFile(f.getAbsolutePath());
            if (bm != null) h.img.setImageBitmap(bm);
            h.lbl.setText("Page " + (pos + 1));
        }

        @Override public int getItemCount() { return pages.size(); }

        static class VH extends RecyclerView.ViewHolder {
            ImageView img; TextView lbl;
            VH(View v) {
                super(v);
                img = v.findViewById(R.id.imgThumb);
                lbl = v.findViewById(R.id.tvPageNum);
            }
        }
    }
}
