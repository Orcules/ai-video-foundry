-- Enable Supabase Realtime on video_jobs table for live Mux status updates
ALTER PUBLICATION supabase_realtime ADD TABLE video_jobs;
